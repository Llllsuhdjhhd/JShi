from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Mapping, Protocol, Sequence
from uuid import uuid4

if TYPE_CHECKING:
    from jshi.memory.contracts import MemoryBatch


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
class ExperienceSegment:
    segment_id: str
    sequence: int
    subject_id: str
    actor_kind: ActorKind
    output_kind: OutputKind
    actor_object_id: str | None
    mentioned_object_ids: tuple[str, ...]
    text_raw: str | None
    state_delta: Mapping[str, object] | None
    objects: Mapping[str, str] | None = None  # 对象映射表：名字/称呼 → object_id（与是否说话无关）
    response_plan: Mapping[str, object] | None = None
    response_statuses: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    occurred_at: datetime = field(default_factory=utc_now)
    status: SegmentStatus = SegmentStatus.ACCEPTED


@dataclass(frozen=True)
class ContextViewState:
    version: int = 0
    context_text: str = ""               # 派生：按 segment_refs + recall_excerpts 渲染
    segment_refs: tuple[str, ...] = ()
    recall_excerpts: tuple[tuple[str, str], ...] = ()
    speaker_object_id: str | None = None
    focused_refs: tuple[str, ...] = ()   # 段级聚焦引用
    last_applied_sequence: int = 0


@dataclass(frozen=True)
class ContextAssessment:
    """05 的上下文补丁：段级删除 / 回忆摘录删除 / 聚焦（省 token，不重写全文）。"""

    remove: tuple[str, ...] = ()       # 段 id 或 memory:ref
    drop_recall: tuple[str, ...] = ()  # 仅回忆摘录（memory:ref），低价值建议删除
    focus: tuple[str, ...] = ()        # 段 id（聚焦）


def empty_context_view() -> ContextViewState:
    return ContextViewState()


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


class ExperienceLedgerPort(Protocol):
    def append_external(
        self,
        subject_id: str,
        *,
        actor_object_id: str,
        text_raw: str,
        objects: Mapping[str, str] | None = None,
        mentioned_object_ids: Sequence[str] = (),
        source_ids: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment: ...

    def append_subject_reply(
        self,
        subject_id: str,
        *,
        text_raw: str,
        objects: Mapping[str, str] | None = None,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        state_delta: Mapping[str, object] | None = None,
        response_plan: Mapping[str, object] | None = None,
        response_statuses: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment: ...

    def append_subject_state(
        self,
        subject_id: str,
        *,
        state_delta: Mapping[str, object],
        objects: Mapping[str, str] | None = None,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        response_plan: Mapping[str, object] | None = None,
        response_statuses: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment: ...

    def append_subject_silent(
        self,
        subject_id: str,
        *,
        objects: Mapping[str, str] | None = None,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        response_plan: Mapping[str, object] | None = None,
        response_statuses: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment: ...

    def append_internal(
        self,
        subject_id: str,
        *,
        text_raw: str | None = None,
        objects: Mapping[str, str] | None = None,
        state_delta: Mapping[str, object] | None = None,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment: ...

    def current_context_view(self, subject_id: str) -> ContextViewState: ...

    def apply_context_assessment(
        self,
        subject_id: str,
        assessment: ContextAssessment | None = None,
        *,
        allow_edit: bool = True,
        recall_excerpts: Sequence[tuple[str, str]] = (),
        speaker_object_id: str | None = None,
        protected_refs: Sequence[str] = (),
    ) -> ContextViewState: ...

    def list_experiences(
        self,
        subject_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[ExperienceSegment, ...]: ...

    def active_window(
        self,
        subject_id: str,
        *,
        limit_chars: int | None = None,
    ) -> tuple[ExperienceSegment, ...]: ...

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
