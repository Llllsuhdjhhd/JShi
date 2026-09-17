from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jshi.models import ResponsePlan

from .port import ActionDispatchResult, ActionPort, RobotActionPort, SpeechPort

if TYPE_CHECKING:
    from jshi.subject.repository import SubjectRepository


@dataclass
class PlaceholderSpeech:
    """语音占位：记下应说的文本，不接真实发音。"""

    def speak(
        self,
        *,
        subject_id: str,
        activity_id: str,
        text: str,
    ) -> None:
        del subject_id, activity_id, text


@dataclass
class PlaceholderRobotAction:
    """肢体动作占位：当前不执行物理动作。与语音分立。"""

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
    speech: SpeechPort = field(default_factory=PlaceholderSpeech)

    def dispatch(
        self,
        *,
        subject_id: str,
        activity_id: str,
        action_text: str,
        model: str | None,
        source_id: str,
        response_plan: ResponsePlan,
    ) -> ActionDispatchResult:
        from jshi.subject.domain import HistoryKind, HistoryRecord

        spoken = response_plan.verbal_text()
        del action_text
        action_id = ""
        speech_triggered = False
        if spoken:
            action = HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.FACT,
                event_type="language_action",
                content={
                    "activity_id": activity_id,
                    "text": spoken,
                    "model": model,
                    "response_statuses": [response_plan.mode],
                    "reason": response_plan.reason,
                },
                source_ids=(source_id,),
            )
            self.repository.add_history(action)
            action_id = action.id
            self.speech.speak(
                subject_id=subject_id,
                activity_id=activity_id,
                text=spoken,
            )
            speech_triggered = True
        robot_triggered = response_plan.has_embodied()
        if robot_triggered:
            self.robot.trigger_embodied_action(
                subject_id=subject_id,
                activity_id=activity_id,
                action_text=next(
                    item.text for item in response_plan.items if item.channel == "embodied"
                ),
                response_statuses=(response_plan.mode, "embodied"),
            )
        return ActionDispatchResult(
            action_id=action_id,
            robot_action_triggered=robot_triggered,
            speech_triggered=speech_triggered,
        )
