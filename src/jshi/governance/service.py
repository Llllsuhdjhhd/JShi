from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from jshi.core import CandidateChange, TruthStatus


@dataclass(frozen=True)
class AuditDecision:
    accepted: tuple[CandidateChange, ...]
    rejected: tuple[CandidateChange, ...]
    reasons: tuple[str, ...] = ()


class GovernanceService:
    """Conservative first-pass guard; richer governance remains pluggable."""

    def review(self, changes: Sequence[CandidateChange]) -> AuditDecision:
        accepted: list[CandidateChange] = []
        rejected: list[CandidateChange] = []
        reasons: list[str] = []
        for change in changes:
            truth_status = change.value.get("truth_status")
            if (
                change.target in {"fact", "identity"}
                and truth_status == TruthStatus.IMAGINED.value
            ):
                rejected.append(change)
                reasons.append(f"{change.id}: imagined content cannot alter {change.target}")
            elif change.target == "identity" and change.significance < 0.8:
                rejected.append(change)
                reasons.append(f"{change.id}: identity significance is too low")
            else:
                accepted.append(change)
        return AuditDecision(tuple(accepted), tuple(rejected), tuple(reasons))

    def compatible(self, api_version: str) -> bool:
        return api_version == "1"
