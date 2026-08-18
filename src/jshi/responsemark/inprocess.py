from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Sequence

from jshi.models import ResponsePlan

from .port import ResponseMarkPort, ResponseMarkResult, evaluate_response_plan

if TYPE_CHECKING:
    from jshi.subject.repository import SubjectRepository


@dataclass
class InProcessResponseMark(ResponseMarkPort):
    """06：只校验并标记 response_plan，不写经历、不改活跃区、不投递记忆。"""

    repository: SubjectRepository

    def mark(
        self,
        subject_id: str,
        activity_id: str,
        response_plan: ResponsePlan,
        *,
        human_override: Sequence[str] | None = None,
    ) -> ResponseMarkResult:
        from jshi.subject.domain import HistoryKind, HistoryRecord

        evaluation = evaluate_response_plan(
            response_plan, human_override=human_override
        )
        updated = self.repository.update_activity(
            activity_id,
            response_statuses=evaluation.final,
        )
        result = replace(evaluation, activity_id=updated.id)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="activity_response_state",
                content={
                    "activity_id": result.activity_id,
                    "recommended": list(result.recommended),
                    "final": list(result.final),
                    "unknown_statuses": list(result.unknown),
                    "illegal_channels": list(result.illegal_channels),
                    "missing_reason": result.missing_reason,
                    "missing_items": result.missing_items,
                    "reason": result.reason,
                    "items": [
                        {"channel": channel, "text": text}
                        for channel, text in result.items
                    ],
                    "source": result.source,
                },
                source_ids=(updated.id,),
            )
        )
        return result
