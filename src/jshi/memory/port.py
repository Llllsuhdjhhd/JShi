from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from jshi.subject.domain import HistoryKind, HistoryRecord
from jshi.subject.repository import SubjectRepository


@dataclass(frozen=True)
class RecalledFragment:
    """One recalled past fragment shown in the current-state working set."""

    event_id: str
    event_type: str
    text: str
    kind: str = "fact"


class MemoryPort(Protocol):
    """Optional, replaceable memory backend.

    Project-internal history satisfies the minimal experiment. External engines
    (for example REMS) may implement the same narrow recall/remember surface
    later; they are not required by the architecture.
    """

    def remember_fact(
        self, subject_id: str, event_type: str, text: str, source_ids: tuple[str, ...] = ()
    ) -> str:
        """Persist a factual fragment; return its id."""

    def recall(
        self, subject_id: str, query: str, *, limit: int = 8
    ) -> Sequence[RecalledFragment]:
        """Return a bounded working set of related past fragments."""


class InProcessHistoryMemory:
    """Minimal memory adapter over the subject fact history store."""

    def __init__(self, repository: SubjectRepository) -> None:
        self._repository = repository

    def remember_fact(
        self, subject_id: str, event_type: str, text: str, source_ids: tuple[str, ...] = ()
    ) -> str:
        record = HistoryRecord(
            subject_id=subject_id,
            kind=HistoryKind.FACT,
            event_type=event_type,
            content={"text": text},
            source_ids=source_ids,
        )
        self._repository.add_history(record)
        return record.id

    def recall(
        self, subject_id: str, query: str, *, limit: int = 8
    ) -> Sequence[RecalledFragment]:
        # Transparent first implementation: recent facts, lightly filtered by overlap.
        recent = self._repository.list_history(
            subject_id, kind=HistoryKind.FACT, limit=max(limit * 3, limit)
        )
        tokens = {token for token in query.lower().split() if token}
        scored: list[tuple[int, HistoryRecord]] = []
        for record in recent:
            text = str(record.content.get("text", ""))
            hay = text.lower()
            score = sum(1 for token in tokens if token in hay) if tokens else 0
            # Keep chronological presence even without lexical hit.
            scored.append((score, record))
        scored.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        chosen = [record for score, record in scored if score > 0][:limit]
        if not chosen:
            chosen = [record for _, record in scored[:limit]]
        return tuple(
            RecalledFragment(
                event_id=record.id,
                event_type=record.event_type,
                text=str(record.content.get("text", "")),
                kind=record.kind.value,
            )
            for record in chosen
        )
