from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Mapping, Sequence

from .port import (
    ExperienceLedgerPort,
    ExperienceSegment,
    ActorKind,
    ConsumerKind,
    MemoryBatch,
    MemoryIngestLedgerEntry,
    OutputKind,
    SegmentStatus,
    new_id,
    utc_now,
)


@dataclass
class _SubjectLedgerState:
    segments: list[ExperienceSegment] = field(default_factory=list)
    cursors: dict[ConsumerKind, int] = field(default_factory=dict)
    ingest_entries: list[MemoryIngestLedgerEntry] = field(default_factory=list)


class InProcessExperienceLedger(ExperienceLedgerPort):
    """最小活动日志：外部输入、主体回复、内部活动都按时间顺序追加。"""

    def __init__(
        self,
        *,
        active_window_chars: int = 2000,
        memory_batch_chars: int = 2000,
        memory_batch_segments: int = 3,
        memory_batch_max_age_seconds: float = 3600,
    ) -> None:
        self.active_window_chars = active_window_chars
        self.memory_batch_chars = memory_batch_chars
        self.memory_batch_segments = memory_batch_segments
        self.memory_batch_max_age_seconds = memory_batch_max_age_seconds
        self._states: dict[str, _SubjectLedgerState] = {}

    def _state(self, subject_id: str) -> _SubjectLedgerState:
        state = self._states.get(subject_id)
        if state is None:
            state = _SubjectLedgerState(
                cursors={
                    ConsumerKind.ACTIVE_ZONE: 0,
                    ConsumerKind.MEMORY: 0,
                    ConsumerKind.AUDIT: 0,
                }
            )
            self._states[subject_id] = state
        return state

    def append_external(
        self,
        subject_id: str,
        *,
        actor_object_id: str,
        text_raw: str,
        mentioned_object_ids: Sequence[str] = (),
        source_ids: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment:
        return self._append(
            subject_id=subject_id,
            actor_kind=ActorKind.EXTERNAL,
            output_kind=OutputKind.EXTERNAL_INPUT,
            actor_object_id=actor_object_id,
            text_raw=text_raw,
            state_delta=None,
            response_statuses=(),
            mentioned_object_ids=mentioned_object_ids,
            source_ids=source_ids,
            occurred_at=occurred_at,
        )

    def append_subject_reply(
        self,
        subject_id: str,
        *,
        text_raw: str,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        state_delta: Mapping[str, object] | None = None,
        response_statuses: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment:
        return self._append(
            subject_id=subject_id,
            actor_kind=ActorKind.SUBJECT,
            output_kind=OutputKind.SUBJECT_REPLY,
            actor_object_id=None,
            text_raw=text_raw,
            state_delta=dict(state_delta) if state_delta else None,
            response_statuses=tuple(dict.fromkeys(response_statuses)),
            mentioned_object_ids=mentioned_object_ids,
            source_ids=source_ids,
            occurred_at=occurred_at,
        )

    def append_subject_state(
        self,
        subject_id: str,
        *,
        state_delta: Mapping[str, object],
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        response_statuses: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment:
        return self._append(
            subject_id=subject_id,
            actor_kind=ActorKind.SUBJECT,
            output_kind=OutputKind.SUBJECT_STATE,
            actor_object_id=None,
            text_raw=None,
            state_delta=dict(state_delta),
            response_statuses=tuple(dict.fromkeys(response_statuses)),
            mentioned_object_ids=mentioned_object_ids,
            source_ids=source_ids,
            occurred_at=occurred_at,
        )

    def append_subject_silent(
        self,
        subject_id: str,
        *,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        response_statuses: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment:
        return self._append(
            subject_id=subject_id,
            actor_kind=ActorKind.SUBJECT,
            output_kind=OutputKind.SUBJECT_SILENT,
            actor_object_id=None,
            text_raw=None,
            state_delta=None,
            response_statuses=tuple(dict.fromkeys(response_statuses)),
            mentioned_object_ids=mentioned_object_ids,
            source_ids=source_ids,
            occurred_at=occurred_at,
        )

    def append_internal(
        self,
        subject_id: str,
        *,
        text_raw: str | None = None,
        state_delta: Mapping[str, object] | None = None,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment:
        return self._append(
            subject_id=subject_id,
            actor_kind=ActorKind.SYSTEM,
            output_kind=OutputKind.INTERNAL,
            actor_object_id=None,
            text_raw=text_raw,
            state_delta=dict(state_delta) if state_delta else None,
            response_statuses=(),
            mentioned_object_ids=mentioned_object_ids,
            source_ids=source_ids,
            occurred_at=occurred_at,
        )

    def _append(
        self,
        *,
        subject_id: str,
        actor_kind: ActorKind,
        output_kind: OutputKind,
        actor_object_id: str | None,
        text_raw: str | None,
        state_delta: Mapping[str, object] | None,
        response_statuses: Sequence[str],
        mentioned_object_ids: Sequence[str],
        source_ids: Sequence[str],
        occurred_at: datetime | None,
    ) -> ExperienceSegment:
        state = self._state(subject_id)
        sequence = self.head_sequence(subject_id) + 1
        segment = ExperienceSegment(
            segment_id=new_id(),
            sequence=sequence,
            subject_id=subject_id,
            actor_kind=actor_kind,
            output_kind=output_kind,
            actor_object_id=actor_object_id,
            mentioned_object_ids=tuple(dict.fromkeys(mentioned_object_ids)),
            text_raw=text_raw,
            state_delta=state_delta,
            response_statuses=tuple(dict.fromkeys(response_statuses)),
            source_ids=tuple(dict.fromkeys(source_ids)),
            occurred_at=occurred_at or utc_now(),
        )
        state.segments.append(segment)
        return segment

    def active_window(
        self,
        subject_id: str,
        *,
        limit_chars: int | None = None,
    ) -> tuple[ExperienceSegment, ...]:
        state = self._state(subject_id)
        start = state.cursors.get(ConsumerKind.ACTIVE_ZONE, 0)
        selected: list[ExperienceSegment] = []
        used = 0
        cap = limit_chars if limit_chars is not None else self.active_window_chars
        for segment in state.segments:
            if segment.sequence <= start:
                continue
            size = len(segment.text_raw or "")
            if used + size > cap and selected:
                break
            selected.append(segment)
            used += size
        return tuple(selected)

    def build_memory_batch(
        self,
        subject_id: str,
        *,
        min_chars: int | None = None,
        min_segments: int | None = None,
        max_age_seconds: float | None = None,
    ) -> MemoryBatch | None:
        state = self._state(subject_id)
        memory_start = state.cursors.get(ConsumerKind.MEMORY, 0)
        pending = [
            segment for segment in state.segments if segment.sequence > memory_start
        ]
        if not pending:
            return None

        chars = sum(len(segment.text_raw or "") for segment in pending)
        required_chars = min_chars or self.memory_batch_chars
        required_segments = min_segments or self.memory_batch_segments
        max_age = (
            max_age_seconds
            if max_age_seconds is not None
            else self.memory_batch_max_age_seconds
        )

        oldest = pending[0]
        age_seconds = (utc_now() - oldest.occurred_at).total_seconds()
        triggered = (
            chars >= required_chars
            or len(pending) >= required_segments
            or age_seconds >= max_age
        )
        if not triggered:
            return None

        first_external = next(
            (
                segment.actor_object_id
                for segment in pending
                if segment.actor_kind == ActorKind.EXTERNAL
                and segment.actor_object_id
            ),
            None,
        )
        object_ids = tuple(
            dict.fromkeys(
                object_id
                for segment in pending
                for object_id in (
                    (
                        segment.actor_object_id
                        if segment.actor_kind == ActorKind.EXTERNAL
                        else None
                    ),
                    *segment.mentioned_object_ids,
                )
                if object_id
            )
        )
        object_sources: dict[str, tuple[str, ...]] = {}
        for segment in pending:
            candidate_ids = []
            if segment.actor_kind == ActorKind.EXTERNAL and segment.actor_object_id:
                candidate_ids.append(segment.actor_object_id)
            candidate_ids.extend(segment.mentioned_object_ids)
            for object_id in dict.fromkeys(candidate_ids):
                object_sources.setdefault(object_id, [])
                object_sources[object_id].append(segment.segment_id)
        source_ids = tuple(
            dict.fromkeys(
                source_id
                for segment in pending
                for source_id in (segment.segment_id, *segment.source_ids)
            )
        )
        return MemoryBatch(
            batch_id=new_id(),
            subject_id=subject_id,
            external_object_id=first_external,
            object_ids=object_ids,
            object_sources={
                object_id: tuple(dict.fromkeys(segment_ids))
                for object_id, segment_ids in object_sources.items()
            },
            from_sequence=oldest.sequence,
            to_sequence=pending[-1].sequence,
            segments=tuple(pending),
            source_ids=source_ids,
        )

    def advance_consumer_cursor(
        self,
        subject_id: str,
        consumer: ConsumerKind,
        through_sequence: int,
    ) -> int:
        state = self._state(subject_id)
        head = self.head_sequence(subject_id)
        if through_sequence > head:
            raise ValueError(
                f"cursor {through_sequence} exceeds head sequence {head}"
            )
        current = state.cursors.get(consumer, 0)
        updated = max(current, through_sequence)
        state.cursors[consumer] = updated
        return updated

    def consumer_cursor(
        self,
        subject_id: str,
        consumer: ConsumerKind,
    ) -> int:
        return self._state(subject_id).cursors.get(consumer, 0)

    def head_sequence(self, subject_id: str) -> int:
        state = self._states.get(subject_id)
        if not state or not state.segments:
            return 0
        return state.segments[-1].sequence

    def safe_trim_point(self, subject_id: str) -> int:
        state = self._state(subject_id)
        if not state.segments:
            return 0
        return min(
            state.cursors.get(ConsumerKind.ACTIVE_ZONE, 0),
            state.cursors.get(ConsumerKind.MEMORY, 0),
            state.cursors.get(ConsumerKind.AUDIT, 0),
        )

    def archive_before(self, subject_id: str, sequence: int) -> int:
        state = self._state(subject_id)
        archived = 0
        for index, segment in enumerate(state.segments):
            if segment.sequence >= sequence:
                break
            if segment.status != SegmentStatus.ARCHIVED:
                state.segments[index] = replace(
                    segment,
                    status=SegmentStatus.ARCHIVED,
                )
                archived += 1
        return archived

    def register_ingest(self, batch: MemoryBatch) -> MemoryIngestLedgerEntry:
        state = self._state(batch.subject_id)
        entry = MemoryIngestLedgerEntry(
            ingest_id=new_id(),
            batch_id=batch.batch_id,
        )
        state.ingest_entries.append(entry)
        return entry

    def mark_ingesting(self, ingest_id: str) -> MemoryIngestLedgerEntry:
        entry = self._ledger_entry(ingest_id)
        updated = replace(entry, status="ingesting", attempts=entry.attempts + 1)
        self._replace_ledger(updated)
        return updated

    def mark_ingested(
        self,
        ingest_id: str,
        *,
        memory_event_ids: Sequence[str] = (),
        stored_marks: Mapping[str, str] | None = None,
    ) -> MemoryIngestLedgerEntry:
        entry = self._ledger_entry(ingest_id)
        updated = replace(
            entry,
            status="ingested",
            attempts=entry.attempts + 1,
            memory_event_ids=tuple(dict.fromkeys(memory_event_ids)),
            stored_marks=dict(stored_marks or {}),
            ingested_at=utc_now(),
            reason="",
        )
        self._replace_ledger(updated)
        return updated

    def mark_failed(
        self,
        ingest_id: str,
        *,
        reason: str,
    ) -> MemoryIngestLedgerEntry:
        entry = self._ledger_entry(ingest_id)
        updated = replace(
            entry,
            status="failed",
            attempts=entry.attempts + 1,
            reason=reason,
        )
        self._replace_ledger(updated)
        return updated

    def list_ingest_entries(
        self,
        subject_id: str,
    ) -> tuple[MemoryIngestLedgerEntry, ...]:
        return tuple(self._state(subject_id).ingest_entries)

    def _ledger_entry(self, ingest_id: str) -> MemoryIngestLedgerEntry:
        for state in self._states.values():
            for entry in state.ingest_entries:
                if entry.ingest_id == ingest_id:
                    return entry
        raise KeyError(ingest_id)

    def _replace_ledger(self, updated: MemoryIngestLedgerEntry) -> None:
        for state in self._states.values():
            for index, entry in enumerate(state.ingest_entries):
                if entry.ingest_id == updated.ingest_id:
                    state.ingest_entries[index] = updated
                    return
        raise KeyError(updated.ingest_id)
