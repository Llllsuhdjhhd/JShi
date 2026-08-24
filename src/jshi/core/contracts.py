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

    主体面未完成现实 / concern 已废除（D-007）：快照不携带、不编造跟进义务。
    """

    subject_id: str
    identity_summary: str
    current_stance: str
    salient_values: tuple[str, ...] = ()
    commitments: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source="empty_subject_state")
    )
