from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Protocol, Sequence

from .contracts import BackendIngestResult, MemoryBatch

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

    def ingest_batch(self, batch: MemoryBatch) -> BackendIngestResult: ...


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

    def ingest_batch(self, batch: MemoryBatch) -> BackendIngestResult:
        from jshi.subject.domain import HistoryKind, HistoryRecord

        memory_event_ids: list[str] = []
        stored_marks: dict[str, list[str]] = {}
        role_ids: list[str] = []
        for experience in batch.experiences:
            object_ids = tuple(dict.fromkeys((experience.objects or {}).values()))
            role_ids.extend(object_id for object_id in object_ids if object_id)

            record = HistoryRecord(
                subject_id=batch.subject_id,
                kind=HistoryKind.FACT,
                event_type=f"memory_{experience.origin}",
                content={
                    "text": experience.text,
                    "object_ids": list(object_ids),
                    "segment_id": experience.segment_id,
                    "origin": experience.origin,
                    "source_ids": list(experience.source_ids),
                },
                source_ids=(
                    *(item for item in (experience.segment_id,) if item),
                    *experience.source_ids,
                ),
            )
            self._repository.add_history(record)
            memory_event_ids.append(record.id)
            if experience.segment_id:
                stored_marks[experience.segment_id] = [record.id]

        return BackendIngestResult(
            subject_id=batch.subject_id,
            stored_marks=stored_marks,
            sealed_event_ids=tuple(memory_event_ids),
            role_ids=tuple(dict.fromkeys(role_ids)),
            unclosed_count=0,
            errors=(),
        )
