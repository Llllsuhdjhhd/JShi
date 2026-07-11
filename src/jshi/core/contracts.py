from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TruthStatus(StrEnum):
    OBSERVED = "observed"
    REPORTED = "reported"
    INFERRED = "inferred"
    REFLECTION = "reflection"
    IMAGINED = "imagined"


class EventKind(StrEnum):
    EXTERNAL_INPUT = "external_input"
    ACTION = "action"
    FEEDBACK = "feedback"
    INNER_ACTIVITY = "inner_activity"
    DERIVED = "derived"
    AUDIT = "audit"


@dataclass(frozen=True)
class Provenance:
    source: str
    source_event_ids: tuple[str, ...] = ()
    method: str | None = None
    model: str | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Event:
    subject_id: str
    kind: EventKind
    content: Mapping[str, Any]
    truth_status: TruthStatus
    provenance: Provenance
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1


@dataclass(frozen=True)
class CandidateChange:
    subject_id: str
    target: str
    operation: str
    value: Mapping[str, Any]
    provenance: Provenance
    confidence: float = 0.5
    significance: float = 0.0
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class SubjectState:
    subject_id: str
    identity_summary: str
    current_stance: str
    salient_values: tuple[str, ...] = ()
    commitments: tuple[str, ...] = ()
    concerns: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source="empty_subject_state")
    )


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    api_version: str = "1"
    category: str = "mind"
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    stateful: bool = False


@dataclass(frozen=True)
class PluginContext:
    subject_id: str
    event: Event
    recent_events: Sequence[Event] = ()


@runtime_checkable
class Plugin(Protocol):
    @property
    def manifest(self) -> PluginManifest: ...

    def contribute(self, context: PluginContext) -> Mapping[str, Any]: ...

    def propose_changes(
        self, context: PluginContext, outcome: Event
    ) -> Sequence[CandidateChange]: ...

    def healthcheck(self) -> bool: ...

    def initialize(self) -> None: ...

    def shutdown(self) -> None: ...

    def export_state(self) -> Mapping[str, Any]: ...

    def migrate(self, previous_version: str, state: Mapping[str, Any]) -> None: ...
