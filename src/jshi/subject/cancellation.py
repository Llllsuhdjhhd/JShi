"""Abandon a stale model request without running concurrent cognition pipelines."""
from contextlib import contextmanager
from contextvars import ContextVar
from queue import Queue, Empty
from threading import BoundedSemaphore, Thread


class TurnCancelled(Exception):
    pass


_allowed = ContextVar('turn_allowed', default=None)
_capacity = BoundedSemaphore(4)


def check():
    allowed = _allowed.get()
    if allowed is not None and not allowed():
        raise TurnCancelled('本轮已被新的播放控制作废')


@contextmanager
def generation_scope(allowed):
    token = _allowed.set(allowed)
    try:
        check()
        yield
    finally:
        _allowed.reset(token)


def invoke(model, request, *, on_reply=None):
    if _allowed.get() is None:
        return model.generate_stream(request, on_reply=on_reply) if on_reply is not None and hasattr(model, 'generate_stream') else model.generate(request)
    check()
    if not _capacity.acquire(blocking=False):
        raise RuntimeError('此前模型请求仍在结束，本次未新增请求，请稍后重试')
    results = Queue(maxsize=1)
    allowed = _allowed.get()
    def callback(value):
        if allowed() and on_reply is not None:
            on_reply(value)
    def run():
        try:
            result = model.generate_stream(request, on_reply=callback) if on_reply is not None and hasattr(model, 'generate_stream') else model.generate(request)
            results.put((True, result))
        except BaseException as exc:
            results.put((False, exc))
        finally:
            _capacity.release()
    # Only the transport call survives cancellation; it cannot write process state.
    Thread(target=run, daemon=True, name='jshi-cancellable-model').start()
    while True:
        check()
        try:
            success, value = results.get(timeout=.025)
        except Empty:
            continue
        check()
        if not success:
            raise value
        return value
