from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class PersonalKind(StrEnum):
    MEMORY = "memory"
    VALUE = "value"
    RELATIONSHIP = "relationship"
    COMMITMENT = "commitment"
    CONCERN = "concern"
    CAPABILITY = "capability"
    AESTHETIC = "aesthetic"
    SELF_UNDERSTANDING = "self_understanding"


class PersonalStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    RELEASED = "released"
    SUPERSEDED = "superseded"


class CognitiveKind(StrEnum):
    PERCEPTION = "perception"
    RECOLLECTION = "recollection"
    INFERENCE = "inference"
    IMAGINATION = "imagination"
    EVALUATION = "evaluation"
    INTENTION = "intention"


class EpistemicStatus(StrEnum):
    APPEARED = "appeared"
    CONSIDERING = "considering"
    PROVISIONAL = "provisional"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUSPENDED = "suspended"
    REVISED = "revised"


class EvidenceKind(StrEnum):
    OBSERVATION = "observation"
    REPORT = "report"
    PERSONAL_MEMORY = "personal_memory"
    COGNITIVE_REASONING = "cognitive_reasoning"
    EXTERNAL_SOURCE = "external_source"
    NONE = "none"


class ActivityKind(StrEnum):
    EXTERNAL = "external"
    INTERNAL = "internal"


class ActivityStatus(StrEnum):
    OPEN = "open"
    WAITING = "waiting"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class HistoryKind(StrEnum):
    FACT = "fact"
    SUBJECT = "subject"


@dataclass(frozen=True)
class PersonalItem:
    subject_id: str
    kind: PersonalKind
    content: str
    source_ids: tuple[str, ...] = ()
    status: PersonalStatus = PersonalStatus.ACTIVE
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Activity:
    subject_id: str
    kind: ActivityKind
    trigger: str
    status: ActivityStatus = ActivityStatus.OPEN
    active_concern_ids: tuple[str, ...] = ()
    intention_ids: tuple[str, ...] = ()
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class CognitiveContent:
    subject_id: str
    activity_id: str
    kind: CognitiveKind
    content: str
    epistemic_status: EpistemicStatus
    evidence_kind: EvidenceKind
    source_ids: tuple[str, ...] = ()
    model: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class StateTransition:
    subject_id: str
    target_type: str
    target_id: str
    from_state: str
    to_state: str
    reason: str
    source_ids: tuple[str, ...] = ()
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class HistoryRecord:
    subject_id: str
    kind: HistoryKind
    event_type: str
    content: Mapping[str, Any]
    source_ids: tuple[str, ...] = ()
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)


ALLOWED_EPISTEMIC_TRANSITIONS: Mapping[EpistemicStatus, frozenset[EpistemicStatus]] = {
    EpistemicStatus.APPEARED: frozenset(
        {
            EpistemicStatus.CONSIDERING,
            EpistemicStatus.REJECTED,
            EpistemicStatus.SUSPENDED,
        }
    ),
    EpistemicStatus.CONSIDERING: frozenset(
        {
            EpistemicStatus.PROVISIONAL,
            EpistemicStatus.ACCEPTED,
            EpistemicStatus.REJECTED,
            EpistemicStatus.SUSPENDED,
        }
    ),
    EpistemicStatus.PROVISIONAL: frozenset(
        {
            EpistemicStatus.ACCEPTED,
            EpistemicStatus.REJECTED,
            EpistemicStatus.SUSPENDED,
            EpistemicStatus.REVISED,
        }
    ),
    EpistemicStatus.ACCEPTED: frozenset(
        {EpistemicStatus.REJECTED, EpistemicStatus.REVISED}
    ),
    EpistemicStatus.SUSPENDED: frozenset(
        {
            EpistemicStatus.CONSIDERING,
            EpistemicStatus.PROVISIONAL,
            EpistemicStatus.REJECTED,
        }
    ),
    EpistemicStatus.REJECTED: frozenset(),
    EpistemicStatus.REVISED: frozenset(),
}
