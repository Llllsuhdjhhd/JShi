"""205 策划 skill：选模板、填参数、写估价。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from jshi.models import ModelRequest
from jshi.tool.contract import AskMode, ToolOrigin, ToolRequest
from jshi.tool.intake import IntakeRecord
from jshi.tool.plan import PlanFailure, PlanResult

from .base import Skill
from .tool_io import tool_skill_request

TOOL_PLAN_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "template": {"type": "string"},
        "params": {"type": "object"},
        "estimate": {
            "type": "object",
            "properties": {
                "time_est_ms": {"type": ["integer", "null"]},
                "cost_est": {"type": ["number", "null"]},
                "benefit": {"type": "string"},
                "downside": {"type": "string"},
                "need_confirm": {"type": "boolean"},
            },
        },
        "expected_result": {"type": "string"},
        "ask": {"enum": ["execute", "propose_only"]},
        "error": {"type": "string"},
    },
    "required": ["ok"],
}


class ToolPlanSkill(Skill[PlanResult]):
    name = "tool_plan"
    version = "v1"
    schema = TOOL_PLAN_SCHEMA
    instruction = """你是匠石的工具策划，不是对说话的那一轮认知。
根据意图选一个定义模板，填参数和估价槽。
不要点名编造目录里没有的模板。
外部路径已经对人说过需求的，定义须与那句一致，不要另起一套。
填不出就失败，写出一句给匠石自己看的原因，不要装成已经在跑。
任务 JSON 含 need、verbal、catalog。"""

    def parse(self, data: Mapping[str, Any]) -> PlanResult:
        if not bool(data.get("ok")):
            error = str(data.get("error") or "").strip() or "策划失败"
            return PlanFailure(error)
        template = str(data.get("template") or "").strip()
        if not template:
            return PlanFailure("未选择模板")
        params = data.get("params") if isinstance(data.get("params"), Mapping) else {}
        raw_est = data.get("estimate") if isinstance(data.get("estimate"), Mapping) else {}
        ask_raw = str(data.get("ask") or AskMode.EXECUTE.value).strip()
        try:
            ask = AskMode(ask_raw)
        except ValueError:
            ask = AskMode.EXECUTE
        return ToolRequest(
            template=template,
            params=dict(params),
            need="",
            expected_result=str(data.get("expected_result") or ""),
            ask=ask,
            meta={
                "estimate": {
                    "time_est_ms": raw_est.get("time_est_ms"),
                    "cost_est": raw_est.get("cost_est"),
                    "benefit": str(raw_est.get("benefit") or ""),
                    "downside": str(raw_est.get("downside") or ""),
                    "need_confirm": bool(raw_est.get("need_confirm", False)),
                }
            },
        )

    def _fallback(self, raw_text: str) -> PlanResult:
        del raw_text
        return PlanFailure("策划失败")

    def plan_intake(
        self,
        intake: IntakeRecord,
        catalog: Sequence[Mapping[str, Any]],
    ) -> PlanResult:
        names = {str(item.get("name") or "") for item in catalog}
        request: ModelRequest = tool_skill_request(
            intake.subject_id,
            {
                "need": intake.need,
                "verbal": intake.verbal,
                "object_id": intake.object_id,
                "catalog": list(catalog),
            },
        )
        planned = self.run(request)
        if isinstance(planned, PlanFailure):
            return planned
        if planned.template not in names:
            return PlanFailure("所选模板不在目录中")
        origin = ToolOrigin.EXTERNAL_05
        raw_origin = (intake.origin or "").strip()
        if raw_origin:
            try:
                origin = ToolOrigin(raw_origin)
            except ValueError:
                origin = ToolOrigin.EXTERNAL_05
        estimate = planned.meta.get("estimate") if isinstance(planned.meta, Mapping) else {}
        return ToolRequest(
            subject_id=intake.subject_id,
            activity_id=intake.activity_id,
            origin=origin,
            need=intake.need,
            template=planned.template,
            params=planned.params,
            expected_result=planned.expected_result,
            ask=planned.ask,
            meta={"estimate": dict(estimate or {})},
        )


class SkillPlanner:
    """把 ToolPlanSkill 接到 ToolPlanner 口。测里仍可注入 RulePlanner。"""

    def __init__(
        self,
        skill: ToolPlanSkill,
        catalog: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.skill = skill
        self.catalog = tuple(catalog or ())

    def plan(self, intake: IntakeRecord) -> PlanResult:
        return self.skill.plan_intake(intake, self.catalog)
