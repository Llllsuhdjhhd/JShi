from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Provenance:
    source: str
    source_event_ids: tuple[str, ...] = ()
    method: str | None = None
    model: str | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class SubjectState:
    """Snapshot passed to the cognitive model for one activity.

    ``concerns`` holds subject-facing open matter texts already stored for this
    subject (未完成现实·主体面). It must not be invented at assemble time.
    """

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
