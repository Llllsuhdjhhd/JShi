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
        from .rems3 import Rems3MemoryBackend

        backend = Rems3MemoryBackend(data_dir=rems_data_dir(data_dir or Path(".jshi")))
    else:
        raise UnknownMemoryBackendError(
            f"未知记忆后端 {name!r}，应为 {BACKEND_INPROCESS} 或 {BACKEND_REMS3}"
        )
    logger.info("memory backend: %s", type(backend).__name__)
    return backend
