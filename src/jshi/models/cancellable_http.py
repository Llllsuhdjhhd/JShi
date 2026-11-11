"""Cancellable HTTP boundary. Worker owns only transport, never business state."""
import multiprocessing
from email.message import Message
from urllib.request import Request
from urllib.error import HTTPError, URLError


def _worker(pipe, spec, timeout):
    from .http_pool import _urlopen_local
    response = None
    try:
        request = Request(spec[0], data=spec[1], headers=spec[2], method=spec[3])
        try:
            response = _urlopen_local(request, timeout=timeout)
        except HTTPError as exc:
            response = exc
        pipe.send(("opened", (response.status, list(response.headers.items()),
                              getattr(response, "reason", ""), getattr(response, "transport", {}))))
        while True:
            method, size = pipe.recv()
            if method == "close":
                return
            pipe.send(("data", getattr(response, method)(size)))
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        try:
            pipe.send(("error", (type(exc).__name__, str(exc))))
        except (EOFError, BrokenPipeError):
            pass
    finally:
        if response is not None:
            response.close()
        pipe.close()


class CancellableResponse:
    def __init__(self, request, timeout):
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_worker, args=(child,
            (request.full_url, request.data, dict(request.header_items()), request.get_method()), timeout), daemon=True)
        self.closed = False
        self.url = request.full_url
        self.process.start()
        child.close()
        try:
            status, headers, self.reason, self.transport = self._receive()
            self.status = self.code = status
            self.headers = Message()
            for key, value in headers:
                self.headers[key] = value
        except BaseException:
            self.close()
            raise

    def _receive(self):
        from jshi.subject.cancellation import check
        try:
            while not self.pipe.poll(.025):
                check()
                if not self.process.is_alive():
                    raise URLError("模型请求进程意外退出")
            check()
            kind, value = self.pipe.recv()
            if kind == "error":
                if value[0] in {"TimeoutError", "socket.timeout"}:
                    raise TimeoutError(value[1])
                raise URLError(value[1])
            return value
        except BaseException:
            self.close()
            raise

    def _read(self, method, size):
        from jshi.subject.cancellation import check
        check()
        self.pipe.send((method, size))
        return self._receive()

    def read(self, size=-1):
        return self._read("read", size)

    def readline(self, size=-1):
        return self._read("readline", size)

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line

    def getcode(self):
        return self.status

    def info(self):
        return self.headers

    def geturl(self):
        return self.url

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.pipe.send(("close", None))
        except (OSError, EOFError):
            pass
        self.process.join(.1)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join()  # Do not release cognition ownership before actual exit.
        self.pipe.close()
        self.process.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_cancellable(request, timeout):
    response = CancellableResponse(request, timeout)
    if response.status >= 400:
        raise HTTPError(request.full_url, response.status, response.reason, response.headers, response)
    return response
