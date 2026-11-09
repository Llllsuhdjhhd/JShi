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
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, getproxies, proxy_bypass, urlopen as _plain_urlopen

_IDLE_PER_HOST = 4
# A prewarmed connection younger than this is assumed still open; older ones may have been closed by the server.
_FRESH_S = 15.0
_idle: dict[tuple[str, int, str], list[tuple[http.client.HTTPSConnection, float]]] = {}
_lock = threading.Lock()
_context = ssl.create_default_context()


class _PooledResponse:
    def __init__(self, key, conn, response, url, transport=None):
        self._key, self._conn, self._response, self.url = key, conn, response, url
        self.status = self.code = response.status
        self.reason, self.headers = response.reason, response.headers
        self.transport = transport or {}

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
            if _park(self._key, conn):
                return
        self._response.close()
        conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _park(key, conn):
    with _lock:
        idle = _idle.setdefault(key, [])
        if len(idle) < _IDLE_PER_HOST:
            idle.append((conn, monotonic()))
            return True
    return False


def _connection(key, timeout):
    """Return (connection, reused, idle seconds); a new connection is not yet connected."""
    with _lock:
        idle = _idle.get(key) or []
        conn, since = idle.pop() if idle else (None, 0.0)
    if conn is not None:
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        conn.timeout = timeout
        return conn, True, monotonic() - since
    return _new_connection(key, timeout), False, None


def _new_connection(key, timeout):
    host, port, proxy = key
    if proxy:
        proxied = urlsplit(proxy)
        conn = http.client.HTTPSConnection(proxied.hostname, proxied.port or 80, timeout=timeout, context=_context)
        conn.set_tunnel(host, port)
        return conn
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=_context)


def _route(url):
    """Pool key and path, or None when the call must stay with plain urllib."""
    parts = urlsplit(url)
    if parts.scheme != 'https':
        return None
    proxy = '' if proxy_bypass(parts.hostname or '') else getproxies().get('https', '')
    if proxy and (urlsplit(proxy).scheme != 'http' or '@' in proxy):
        return None  # Authenticated or SOCKS proxies stay with urllib.
    path = parts.path or '/'
    if parts.query:
        path += '?' + parts.query
    return (parts.hostname or '', parts.port or 443, proxy), path


def prewarm(url: str, timeout: float = 5, count: int = 1) -> dict:
    """Keep ``count`` fresh idle TCP + TLS connections ready for concurrent calls to one host."""
    route = _route(url)
    if route is None:
        return {'status': 'unsupported'}
    key = route[0]
    count = max(1, min(count, _IDLE_PER_HOST))
    with _lock:
        idle = _idle.get(key) or []
        fresh = [since for _, since in idle if monotonic() - since < _FRESH_S]
    if len(fresh) >= count:
        return {'status': 'fresh_idle', 'idle_s': round(monotonic() - max(fresh), 1)}
    connect_ms = []
    for _ in range(count - len(fresh)):
        conn = _new_connection(key, timeout)
        started = monotonic()
        try:
            conn.connect()
        except OSError as exc:
            conn.close()
            return {'status': 'failed', 'error_type': type(exc).__name__, 'connected': len(connect_ms)}
        connect_ms.append(round((monotonic() - started) * 1000, 1))
        with _lock:
            idle = _idle.setdefault(key, [])
            if len(idle) >= _IDLE_PER_HOST:
                old, _ = idle.pop(0)
                old.close()
            idle.append((conn, monotonic()))
    return {'status': 'connected', 'connect_ms': connect_ms[0] if len(connect_ms) == 1 else connect_ms}


def urlopen(request: Request, timeout: float = 60):
    """Drop-in for ``urllib.request.urlopen`` that reuses HTTPS connections."""
    if not isinstance(request, Request):
        return _plain_urlopen(request, timeout=timeout)
    route = _route(request.full_url)
    if route is None:
        return _plain_urlopen(request, timeout=timeout)
    key, path = route
    headers = {}
    for name, value in [('Host', urlsplit(request.full_url).netloc),
                        ('User-Agent', 'Python-urllib/%d.%d' % sys.version_info[:2]),
                        ('Connection', 'keep-alive'), *request.header_items()]:
        headers[name.title()] = value  # urllib stores 'User-agent'; avoid sending two copies.
    stale = False
    for attempt in range(2):
        # The retry never takes another idle connection: it may be just as stale.
        conn, reused, idle_s = _connection(key, timeout) if attempt == 0 else (_new_connection(key, timeout), False, None)
        connect_ms = None
        try:
            if not reused:
                started = monotonic()
                conn.connect()
                connect_ms = round((monotonic() - started) * 1000, 1)
            sent = monotonic()
            conn.request(request.get_method(), path, body=request.data, headers=headers)
            response = conn.getresponse()
            headers_ms = round((monotonic() - sent) * 1000, 1)
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            if isinstance(exc, TimeoutError):
                raise
            # An idle kept-alive connection may have been closed by the server or a
            # gateway (reset, TLS EOF); retry once on a fresh one.
            if reused and attempt == 0:
                stale = True
                continue
            raise URLError(exc) from None
        transport = {'reused': reused, 'connect_ms': connect_ms, 'headers_ms': headers_ms,
                     'idle_s': round(idle_s, 1) if idle_s is not None else None, 'stale_retry': stale}
        wrapped = _PooledResponse(key, conn, response, request.full_url, transport)
        if response.status >= 400:
            raise HTTPError(request.full_url, response.status, response.reason, response.headers, wrapped)
        return wrapped
    raise RuntimeError('unreachable')
