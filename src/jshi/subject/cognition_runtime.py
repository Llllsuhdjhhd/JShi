"""One execution owner per repository and subject, including preparation."""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from threading import Event, Lock
import hashlib
import os

_registry = {}
_registry_lock = Lock()

class CognitionRuntime:
    def __init__(self, path):
        self.path = path
        self.lock = Lock()
        self.cancelled = Event()
        self.state = "idle"

    def interrupt(self):
        if self.state != "idle":
            self.state = "stopping"
            self.cancelled.set()

    @contextmanager
    def occupy(self):
        from .cancellation import check, generation_scope
        while not self.lock.acquire(timeout=.025):
            check()
        file = None
        locked = False
        try:
            check()
            self.cancelled = Event()
            self.state = "waiting"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            file = self.path.open("a+b")
            file.seek(0, 2)
            if not file.tell():
                file.write(b"0"); file.flush()
            while not locked:
                check()
                if self.cancelled.wait(.025):
                    from .cancellation import TurnCancelled
                    raise TurnCancelled("认知已取消")
                try:
                    file.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except (OSError, BlockingIOError):
                    continue
            self.state = "running"
            with generation_scope(lambda: not self.cancelled.is_set()):
                yield
        finally:
            if file:
                if locked:
                    file.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
                file.close()
            self.state = "idle"
            self.lock.release()

def runtime_for(process, subject_id):
    repository = Path(process.repository.path).resolve()
    key = (str(repository).casefold(), subject_id)
    with _registry_lock:
        if key not in _registry:
            name = hashlib.sha256((key[0] + "|" + subject_id).encode()).hexdigest()[:24]
            _registry[key] = CognitionRuntime(repository.parent / ".runtime" / (name + ".lock"))
        return _registry[key]

def serialized(fn):
    @wraps(fn)
    def wrapped(process, subject_id, *args, **kwargs):
        with runtime_for(process, subject_id).occupy():
            return fn(process, subject_id, *args, **kwargs)
    return wrapped
