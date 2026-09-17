from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


@dataclass(frozen=True)
class EvaluationEvent:
    event_id: str
    subject_id: str
    activity_id: str
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source_ids: tuple[str, ...] = ()
    occurred_at: datetime = field(default_factory=utc_now)


class EvaluationEventPort(Protocol):
    """事件总线端口。有效性分析不走这里。"""

    def emit(self, event: EvaluationEvent) -> None: ...


class EvaluationCollectorPort(Protocol):
    def collect(
        self,
        subject_id: str | None = None,
        *,
        since: datetime | None = None,
    ) -> tuple[EvaluationEvent, ...]: ...
