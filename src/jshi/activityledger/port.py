from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Mapping, Protocol, Sequence
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class ConsumerKind(StrEnum):
    ACTIVE_ZONE = "active_zone"
    MEMORY = "memory"
    AUDIT = "audit"


class SegmentStatus(StrEnum):
    ACCEPTED = "accepted"
    CONSUMED = "consumed"
    ARCHIVED = "archived"


class ActorKind(StrEnum):
    EXTERNAL = "external"
    SUBJECT = "subject"
    SYSTEM = "system"


class OutputKind(StrEnum):
    EXTERNAL_INPUT = "external_input"
    SUBJECT_REPLY = "subject_reply"
    SUBJECT_STATE = "subject_state"
    SUBJECT_SILENT = "subject_silent"
    INTERNAL = "internal"


@dataclass(frozen=True)
class ActivitySegment:
    segment_id: str
    sequence: int
    subject_id: str
    actor_kind: ActorKind
    output_kind: OutputKind
    actor_object_id: str | None
    mentioned_object_ids: tuple[str, ...]
    text_raw: str | None
    state_delta: Mapping[str, object] | None
    response_statuses: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    occurred_at: datetime = field(default_factory=utc_now)
    status: SegmentStatus = SegmentStatus.ACCEPTED


@dataclass(frozen=True)
class MemoryBatch:
    batch_id: str
    subject_id: str
    external_object_id: str | None
    object_ids: tuple[str, ...]
    from_sequence: int
    to_sequence: int
    segments: tuple[ActivitySegment, ...]
    source_ids: tuple[str, ...]
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class MemoryIngestLedgerEntry:
    ingest_id: str
    batch_id: str
    status: str = "pending"
    attempts: int = 0
    memory_event_ids: tuple[str, ...] = ()
    stored_marks: Mapping[str, str] = field(default_factory=dict)
    reason: str = ""
    ingested_at: datetime | None = None
    archived_at: datetime | None = None


class ActivityLedgerPort(Protocol):
    def append_external(
        self,
        subject_id: str,
        *,
        actor_object_id: str,
        text_raw: str,
        mentioned_object_ids: Sequence[str] = (),
        source_ids: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ActivitySegment: ...

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
    ) -> ActivitySegment: ...

    def append_subject_state(
        self,
        subject_id: str,
        *,
        state_delta: Mapping[str, object],
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        response_statuses: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ActivitySegment: ...

    def append_subject_silent(
        self,
        subject_id: str,
        *,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        response_statuses: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ActivitySegment: ...

    def append_internal(
        self,
        subject_id: str,
        *,
        text_raw: str | None = None,
        state_delta: Mapping[str, object] | None = None,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ActivitySegment: ...

    def active_window(
        self,
        subject_id: str,
        *,
        limit_chars: int | None = None,
    ) -> tuple[ActivitySegment, ...]: ...

    def build_memory_batch(
        self,
        subject_id: str,
        *,
        min_chars: int | None = None,
        min_segments: int | None = None,
        max_age_seconds: float | None = None,
    ) -> MemoryBatch | None: ...

    def advance_consumer_cursor(
        self,
        subject_id: str,
        consumer: ConsumerKind,
        through_sequence: int,
    ) -> int: ...

    def consumer_cursor(
        self,
        subject_id: str,
        consumer: ConsumerKind,
    ) -> int: ...

    def head_sequence(self, subject_id: str) -> int: ...

    def safe_trim_point(self, subject_id: str) -> int: ...

    def register_ingest(self, batch: MemoryBatch) -> MemoryIngestLedgerEntry: ...

    def mark_ingesting(self, ingest_id: str) -> MemoryIngestLedgerEntry: ...

    def mark_ingested(
        self,
        ingest_id: str,
        *,
        memory_event_ids: Sequence[str] = (),
        stored_marks: Mapping[str, str] | None = None,
    ) -> MemoryIngestLedgerEntry: ...

    def mark_failed(
        self,
        ingest_id: str,
        *,
        reason: str,
    ) -> MemoryIngestLedgerEntry: ...
