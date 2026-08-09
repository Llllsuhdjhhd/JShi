from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from jshi.subject.domain import Activity, HistoryRecord


@dataclass(frozen=True)
class ContinuityFinding:
    """一次连续性检验的发现。"""

    severity: str  # info | warning | error
    message: str
    evidence_ids: tuple[str, ...] = ()


class ContinuityCheckPort(Protocol):
    """治理与连续性检验：检查经历承认、承诺、关系、价值与变化理由。"""

    def check(
        self,
        subject_id: str,
        activity: Activity,
        fact: HistoryRecord,
        attribution: object,
    ) -> tuple[ContinuityFinding, ...]: ...


class PlaceholderContinuityCheck:
    """占位实现：不检查，返回空发现。"""

    name = "placeholder-governance"

    def check(
        self,
        subject_id: str,
        activity: Activity,
        fact: HistoryRecord,
        attribution: object,
    ) -> tuple[ContinuityFinding, ...]:
        return ()
