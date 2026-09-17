from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class ActivityCloseResult:
    activity_id: str
    final_activity_status: str
    transition_id: str = ""
    handoff_to_memory_control: bool = True


class ActivityClosePort(Protocol):
    def close(
        self,
        subject_id: str,
        activity_id: str,
        *,
        final_response_statuses: Sequence[str],
        action_id: str,
        reason: str,
    ) -> ActivityCloseResult: ...
