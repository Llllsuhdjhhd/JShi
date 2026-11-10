"""按启动环境选择记忆后端。缺省进程内；rems3 缺包则失败，不静默退回。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .backend import InProcessMemoryBackend, MemoryBackendPort

if TYPE_CHECKING:
    from jshi.subject.repository import SubjectRepository

logger = logging.getLogger("jshi.memory")

BACKEND_INPROCESS = "inprocess"
BACKEND_REMS3 = "rems3"


class UnknownMemoryBackendError(ValueError):
    """``JSHI_MEMORY_BACKEND`` 不是 inprocess / rems3。"""


class MemoryStorageInUseError(RuntimeError):
    """Another client owns the local vector store; never remove its lock."""


def _storage_in_use(path: Path) -> MemoryStorageInUseError:
    return MemoryStorageInUseError(
        f"记忆库正被另一个进程使用：{path.resolve()}。\n"
        "本次启动已退出。请使用已运行的服务，或先正常退出它后再启动。"
    )


def check_rems_storage_available(data_dir: Path) -> None:
    """Early nonblocking probe only; Qdrant's own lifetime lock remains authoritative."""
    path = data_dir / 'qdrant'
    lock_file = path / '.lock'
    if not lock_file.is_file():
        return  # No directory/file creation just to check a first startup.
    import portalocker
    with lock_file.open('r+b') as handle:
        try:
            portalocker.lock(handle, portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING)
        except portalocker.exceptions.LockException:
            raise _storage_in_use(path) from None
        else:
            portalocker.unlock(handle)


def memory_backend_name() -> str:
    raw = (os.getenv("JSHI_MEMORY_BACKEND") or BACKEND_INPROCESS).strip().lower()
    return raw or BACKEND_INPROCESS


def rems_data_dir(data_dir: Path) -> Path:
    explicit = os.getenv("JSHI_REMS_DATA_DIR")
    if explicit:
        return Path(explicit)
    return Path(data_dir) / "rems"


def build_memory_backend(
    repository: SubjectRepository,
    data_dir: Path | None = None,
) -> MemoryBackendPort:
    name = memory_backend_name()
    if name == BACKEND_INPROCESS:
        backend: MemoryBackendPort = InProcessMemoryBackend(repository)
    elif name == BACKEND_REMS3:
        storage = rems_data_dir(data_dir or Path('.jshi'))
        check_rems_storage_available(storage)
        from .rems3 import Rems3MemoryBackend

        try:
            backend = Rems3MemoryBackend(data_dir=storage)
        except RuntimeError as exc:
            # Another process may acquire the lock after the early check.
            # Preserve unrelated startup failures instead of masking them.
            if 'is already accessed by another instance of Qdrant client' not in str(exc):
                raise
            raise _storage_in_use(storage / 'qdrant') from None
    else:
        raise UnknownMemoryBackendError(
            f"未知记忆后端 {name!r}，应为 {BACKEND_INPROCESS} 或 {BACKEND_REMS3}"
        )
    logger.info("memory backend: %s", type(backend).__name__)
    return backend
