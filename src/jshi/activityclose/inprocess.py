from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from .port import ActivityClosePort, ActivityCloseResult

if TYPE_CHECKING:
    from jshi.subject.repository import SubjectRepository


@dataclass
class InProcessActivityClose(ActivityClosePort):
    """07 最小实现：只负责活动最终状态与审计。"""

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
        )
