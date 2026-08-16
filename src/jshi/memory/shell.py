from __future__ import annotations

from typing import Sequence

from jshi.experienceledger import MemoryBatch
from jshi.memorycontrol import MemoryIngestResult

from .backend import MemoryBackendPort
from .port import RecalledFragment


class MemoryShell:
    """09 薄壳：主流程只依赖此端口，内部适配可替换后端。"""

    name = "memory-shell"

    def __init__(self, backend: MemoryBackendPort) -> None:
        self._backend = backend

    def remember_fact(
        self,
        subject_id: str,
        event_type: str,
        text: str,
        source_ids: tuple[str, ...] = (),
    ) -> str:
        return self._backend.remember_fact(
            subject_id,
            event_type,
            text,
            source_ids,
        )

    def recall(
        self,
        subject_id: str,
        query: str,
        *,
        limit: int | None = None,
        object_id: str | None = None,
        level: int = 1,
        anchor_event_ids: tuple[str, ...] = (),
    ) -> Sequence[RecalledFragment]:
        return self._backend.recall(
            subject_id,
            query,
            limit=limit,
            object_id=object_id,
            level=level,
            anchor_event_ids=anchor_event_ids,
        )

    def ingest_batch(self, batch: MemoryBatch) -> MemoryIngestResult:
        return self._backend.ingest_batch(batch)
