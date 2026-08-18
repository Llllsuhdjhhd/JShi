from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from .port import ActivityClosePort, ActivityCloseResult

if TYPE_CHECKING:
    from jshi.subject.repository import SubjectRepository


_TERMINAL = frozenset({"completed", "abandoned"})


@dataclass
class InProcessActivityClose(ActivityClosePort):
    """07：关闭活动并审计。不写经历、不改活跃区、不投递记忆。"""

    repository: SubjectRepository

    def close(
        self,
        subject_id: str,
        activity_id: str,
        *,
        final_response_statuses: Sequence[str],
        action_id: str,
        reason: str,
    ) -> ActivityCloseResult:
        from jshi.subject.domain import ActivityStatus, HistoryKind, HistoryRecord

        current = self.repository.get_activity(activity_id)
        if current.status.value in _TERMINAL:
            return ActivityCloseResult(
                activity_id=current.id,
                final_activity_status=current.status.value,
                transition_id=self._latest_transition_id(activity_id),
                handoff_to_memory_control=False,
            )

        completed = self.repository.update_activity(
            activity_id,
            status=ActivityStatus.COMPLETED,
            response_statuses=tuple(final_response_statuses),
            reason=reason,
        )
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="activity_completed",
                content={
                    "activity_id": completed.id,
                    "to": completed.status.value,
                    "reason": reason,
                    "response_statuses": list(completed.response_statuses),
                },
                source_ids=tuple(item for item in (action_id,) if item),
            )
        )
        return ActivityCloseResult(
            activity_id=completed.id,
            final_activity_status=completed.status.value,
            transition_id=self._latest_transition_id(activity_id),
            handoff_to_memory_control=True,
        )

    def _latest_transition_id(self, activity_id: str) -> str:
        transitions = self.repository.list_transitions(activity_id)
        return transitions[-1].id if transitions else ""
