"""Local diagnostic spans. Never included in transcripts or model material."""
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from time import monotonic, time
import logging
from functools import wraps
import inspect

_phases = ContextVar('voice_diagnostic_phases', default=None)


def report_safely(report, event, data):
    if report is not None:
        try:
            report(event, data)
        except Exception:
            logging.getLogger(__name__).exception('Voice timing report failed')


@contextmanager
def collect_phases(values):
    token = _phases.set(values)
    try:
        yield
    finally:
        _phases.reset(token)


@contextmanager
def phase(name):
    values = _phases.get()
    if values is None:
        yield
        return
    start = monotonic()
    try:
        yield
    finally:
        values[name] = values.get(name, 0.0) + (monotonic() - start) * 1000


@contextmanager
def stage(report, name, **data):
    """One completed/failed span; monotonic duration plus wall-clock anchors."""
    start, at = monotonic(), time() * 1000
    status = 'completed'
    try:
        yield
    except BaseException as exc:
        status = type(exc).__name__
        raise
    finally:
        report_safely(report, 'voice_input_stage', {**data, 'stage': name,
            'started_at_ms': round(at, 3), 'finished_at_ms': round(time()*1000, 3),
            'elapsed_ms': round((monotonic()-start)*1000, 3), 'status': status})


@contextmanager
def timed_lock(lock, name):
    with phase(name):
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


@asynccontextmanager
async def async_lock(lock, report, name, **data):
    with stage(report, name, **data):
        await lock.acquire()
    try:
        yield
    finally:
        lock.release()


def finish_phase(name, started):
    values = _phases.get()
    if values is not None:
        values[name] = values.get(name, 0.0) + (monotonic()-started)*1000


def input_stage(name):
    """Trace an existing operation; arguments and results never enter the log."""
    def decorate(fn):
        parameter = tuple(inspect.signature(fn).parameters)[1]
        def context(owner, value, kwargs):
            rows = value if isinstance(value, (tuple, list)) else (value,)
            ids = [r if isinstance(r, str) else getattr(r, 'input_id', '') for r in rows]
            return stage(getattr(owner, 'record', None), name, input_ids=[i for i in ids if i],
                         source_ranges=[[r.start_ms, r.end_ms] for r in rows
                                        if hasattr(r, 'start_ms') and hasattr(r, 'end_ms')],
                         **({'wait': kwargs['wait']} if 'wait' in kwargs else {}))
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def run(owner, *args, **kwargs):
                with context(owner, args[0] if args else kwargs.get(parameter), kwargs):
                    return await fn(owner, *args, **kwargs)
        else:
            @wraps(fn)
            def run(owner, *args, **kwargs):
                with context(owner, args[0] if args else kwargs.get(parameter), kwargs):
                    return fn(owner, *args, **kwargs)
        return run
    return decorate
