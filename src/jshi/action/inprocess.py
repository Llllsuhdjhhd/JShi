from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from .port import ActionDispatchResult, ActionPort, RobotActionPort

if TYPE_CHECKING:
    from jshi.subject.repository import SubjectRepository


@dataclass
class PlaceholderRobotAction:
    """机器人动作占位：当前不执行物理动作。"""

    def trigger_embodied_action(
        self,
        *,
        subject_id: str,
        activity_id: str,
        action_text: str,
        response_statuses: Sequence[str],
    ) -> None:
        del subject_id, activity_id, action_text, response_statuses


@dataclass
class InProcessActionRouter(ActionPort):
    repository: SubjectRepository
    robot: RobotActionPort

    def dispatch(
        self,
        *,
        subject_id: str,
        activity_id: str,
        action_text: str,
        model: str | None,
        source_id: str,
        response_statuses: Sequence[str],
    ) -> ActionDispatchResult:
        from jshi.subject.domain import HistoryKind, HistoryRecord

        action = HistoryRecord(
            subject_id=subject_id,
            kind=HistoryKind.FACT,
            event_type="language_action",
            content={
                "activity_id": activity_id,
                "text": action_text,
                "model": model,
                "response_statuses": list(response_statuses),
            },
            source_ids=(source_id,),
        )
        self.repository.add_history(action)
        robot_triggered = "embodied" in response_statuses
        if robot_triggered:
            self.robot.trigger_embodied_action(
                subject_id=subject_id,
                activity_id=activity_id,
                action_text=action_text,
                response_statuses=response_statuses,
            )
        return ActionDispatchResult(
            action_id=action.id,
            robot_action_triggered=robot_triggered,
        )
