from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from jshi.models import ResponsePlan


@dataclass(frozen=True)
class ActionDispatchResult:
    action_id: str
    robot_action_triggered: bool = False
    speech_triggered: bool = False


class SpeechPort(Protocol):
    """把 verbal 文本说出来。与肢体动作分立，本期占位。"""

    def speak(
        self,
        *,
        subject_id: str,
        activity_id: str,
        text: str,
    ) -> None: ...


class RobotActionPort(Protocol):
    def trigger_embodied_action(
        self,
        *,
        subject_id: str,
        activity_id: str,
        action_text: str,
        response_statuses: Sequence[str],
    ) -> None: ...


class ActionPort(Protocol):
    def dispatch(
        self,
        *,
        subject_id: str,
        activity_id: str,
        action_text: str,
        model: str | None,
        source_id: str,
        response_plan: ResponsePlan,
    ) -> ActionDispatchResult: ...
