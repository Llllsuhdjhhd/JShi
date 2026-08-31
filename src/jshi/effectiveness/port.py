"""有效性分析系统端口。不是 `jshi.evaluation` 的事件总线。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Protocol
from uuid import uuid4

from jshi.models import MemoryRatings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


@dataclass(frozen=True)
class EffectivenessReport:
    report_id: str
    subject_id: str
    analyzer: str
    created_at: datetime
    materials_ref: str = ""
    findings: Mapping[str, object] = field(default_factory=dict)
    strategy: Mapping[str, object] = field(default_factory=dict)
    model_tag: str = ""
    analyzer_version: str = ""


class EffectivenessPort(Protocol):
    def record_ratings(
        self,
        subject_id: str,
        activity_id: str,
        ratings: MemoryRatings,
    ) -> None: ...

    def pending_gap_query(self, subject_id: str) -> str: ...

    def consume_gap(self, subject_id: str) -> None: ...

    def run_due(self, subject_id: str) -> None: ...
