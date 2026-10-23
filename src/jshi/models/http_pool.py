"""Keep-alive HTTPS for model calls.

`urllib.request.urlopen` opens a new TCP + TLS connection per call. When the
TLS handshake is slow (security software, network gateways), every model call
pays it again; reusing connections pays it once per host.
"""
from __future__ import annotations

import http.client
import ssl
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, getproxies, proxy_bypass, urlopen as _plain_urlopen

_IDLE_PER_HOST = 4
_idle: dict[tuple[str, int, str], list[http.client.HTTPSConnection]] = {}
_lock = threading.Lock()
_context = ssl.create_default_context()


class _PooledResponse:
    def __init__(self, key, conn, response, url):
        self._key, self._conn, self._response, self.url = key, conn, response, url
        self.status = self.code = response.status
        self.reason, self.headers = response.reason, response.headers

    def read(self, *args):
        return self._response.read(*args)

    def readline(self, *args):
        return self._response.readline(*args)

    def __iter__(self):
        return iter(self._response)

    def getcode(self):
        return self.status

    def info(self):
        return self.headers

    def geturl(self):
        return self.url

    def close(self):
        conn, self._conn = self._conn, None
        if conn is None:
            return
        # Only a fully read response leaves the connection ready for the next request.
        if self._response.isclosed() and not self._response.will_close:
            with _lock:
                idle = _idle.setdefault(self._key, [])
                if len(idle) < _IDLE_PER_HOST:
                    idle.append(conn)
                    return
        self._response.close()
        conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _connection(key, timeout):
    with _lock:
        idle = _idle.get(key) or []
        conn = idle.pop() if idle else None
    if conn is not None:
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        conn.timeout = timeout
        return conn, True
    host, port, proxy = key
    if proxy:
        proxied = urlsplit(proxy)
        conn = http.client.HTTPSConnection(proxied.hostname, proxied.port or 80, timeout=timeout, context=_context)
        conn.set_tunnel(host, port)
        return conn, False
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=_context), False


def urlopen(request: Request, timeout: float = 60):
    """Drop-in for ``urllib.request.urlopen`` that reuses HTTPS connections."""
    if not isinstance(request, Request):
        return _plain_urlopen(request, timeout=timeout)
    parts = urlsplit(request.full_url)
    if parts.scheme != 'https':
        return _plain_urlopen(request, timeout=timeout)
    proxy = '' if proxy_bypass(parts.hostname or '') else getproxies().get('https', '')
    if proxy and (urlsplit(proxy).scheme != 'http' or '@' in proxy):
        return _plain_urlopen(request, timeout=timeout)  # Authenticated or SOCKS proxies stay with urllib.
    key = (parts.hostname or '', parts.port or 443, proxy)
    path = parts.path or '/'
    if parts.query:
        path += '?' + parts.query
    headers = {}
    for name, value in [('Host', parts.netloc), ('User-Agent', 'Python-urllib/%d.%d' % sys.version_info[:2]),
                        ('Connection', 'keep-alive'), *request.header_items()]:
        headers[name.title()] = value  # urllib stores 'User-agent'; avoid sending two copies.
    for attempt in range(2):
        conn, reused = _connection(key, timeout)
        try:
            conn.request(request.get_method(), path, body=request.data, headers=headers)
            response = conn.getresponse()
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError) as exc:
            conn.close()
            # An idle kept-alive connection may have been closed by the server; retry once on a fresh one.
            if reused and attempt == 0:
                continue
            raise URLError(exc) from None
        except OSError as exc:
            conn.close()
            if isinstance(exc, TimeoutError):
                raise
            raise URLError(exc) from None
        wrapped = _PooledResponse(key, conn, response, request.full_url)
        if response.status >= 400:
            raise HTTPError(request.full_url, response.status, response.reason, response.headers, wrapped)
        return wrapped
    raise RuntimeError('unreachable')
