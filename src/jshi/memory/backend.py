from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Protocol, Sequence

from jshi.experienceledger import MemoryBatch
from jshi.memorycontrol import MemoryIngestResult

if TYPE_CHECKING:
    from jshi.subject.repository import SubjectRepository

    from .port import RecalledFragment


class MemoryBackendPort(Protocol):
    """09 后端适配端口；不得新建对象系统，对象 id 一律使用 01。"""

    def remember_fact(
        self,
        subject_id: str,
        event_type: str,
        text: str,
        source_ids: tuple[str, ...] = (),
    ) -> str: ...

    def recall(
        self,
        subject_id: str,
        query: str,
        *,
        limit: int | None = None,
        object_id: str | None = None,
        level: int = 1,
        anchor_event_ids: tuple[str, ...] = (),
    ) -> Sequence[RecalledFragment]: ...

    def ingest_batch(self, batch: MemoryBatch) -> MemoryIngestResult: ...


@dataclass(frozen=True)
class _IngestedRecord:
    segment_id: str
    memory_event_id: str


class InProcessMemoryBackend:
    """最小后端：把 MemoryBatch 落为事实历史，不维护角色/对象系统。"""

    def __init__(self, repository: SubjectRepository) -> None:
        self._repository = repository
        from .port import InProcessHistoryMemory

        self._history = InProcessHistoryMemory(repository)

    def remember_fact(
        self,
        subject_id: str,
        event_type: str,
        text: str,
        source_ids: tuple[str, ...] = (),
    ) -> str:
        return self._history.remember_fact(
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
        return self._history.recall(
            subject_id,
            query,
            limit=limit,
            object_id=object_id,
            level=level,
            anchor_event_ids=anchor_event_ids,
        )

    def ingest_batch(self, batch: MemoryBatch) -> MemoryIngestResult:
        import json

        from jshi.subject.domain import HistoryKind, HistoryRecord

        memory_event_ids: list[str] = []
        stored_marks: dict[str, str] = {}
        for segment in batch.segments:
            if segment.text_raw is not None:
                text = segment.text_raw
            elif segment.state_delta is not None:
                text = json.dumps(segment.state_delta, ensure_ascii=False)
            else:
                text = ""

            record = HistoryRecord(
                subject_id=batch.subject_id,
                kind=HistoryKind.FACT,
                event_type=f"memory_{segment.output_kind.value}",
                content={
                    "text": text,
                    "object_ids": list(batch.object_ids),
                    "segment_id": segment.segment_id,
                    "actor_kind": segment.actor_kind.value,
                    "source_ids": list(segment.source_ids),
                },
                source_ids=(segment.segment_id, *segment.source_ids),
            )
            self._repository.add_history(record)
            memory_event_ids.append(record.id)
            stored_marks[segment.segment_id] = record.id

        return MemoryIngestResult(
            consumed_through_sequence=batch.to_sequence,
            memory_event_ids=tuple(memory_event_ids),
            stored_marks=stored_marks,
        )
