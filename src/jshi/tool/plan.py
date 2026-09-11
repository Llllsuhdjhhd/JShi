"""205 策划口：测里用规则；主流程注入 SkillPlanner。

RulePlanner：need 够长 → echo；过短或空 → PlanFailure。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Union

from .contract import AskMode, ToolOrigin, ToolRequest
from .hang import NEED_MIN_CHARS
from .intake import IntakeRecord


@dataclass(frozen=True)
class PlanFailure:
    message: str


PlanResult = Union[ToolRequest, PlanFailure]


class ToolPlanner(Protocol):
    def plan(self, intake: IntakeRecord) -> PlanResult: ...


class RulePlanner:
    """规则策划：默认 echo，params 空，ask=execute。"""

    def plan(self, intake: IntakeRecord) -> PlanResult:
        need = (intake.need or "").strip()
        if len(need) < NEED_MIN_CHARS:
            return PlanFailure("need 过短，无法策划")
        origin = ToolOrigin.EXTERNAL_05
        raw_origin = (intake.origin or "").strip()
        if raw_origin:
            try:
                origin = ToolOrigin(raw_origin)
            except ValueError:
                origin = ToolOrigin.EXTERNAL_05
        return ToolRequest(
            subject_id=intake.subject_id,
            activity_id=intake.activity_id,
            origin=origin,
            need=need,
            template="echo",
            params={},
            ask=AskMode.EXECUTE,
        )
