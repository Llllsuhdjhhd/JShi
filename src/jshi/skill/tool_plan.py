"""205 策划 skill：选模板、填参数、写估价；大目录时先检索再点名。"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Callable, Mapping, Sequence, Union

from jshi.models import ModelRequest
from jshi.tool.contract import (
    AskMode,
    CreateToolSpec,
    MAX_BUDGET,
    ToolOrigin,
    ToolRequest,
    budget_to_dict,
    new_id,
    normalize_tool_name,
)
from jshi.tool.discovery import (
    LOOKUP_MAX_ROUNDS,
    LookupAsk,
    ToolIndex,
    parse_lookup,
)
from jshi.tool.intake import IntakeRecord
from jshi.tool.plan import PlanFailure, PlanResult, ToolPlan, ToolStep

from .base import Skill
from .tool_io import tool_skill_request

logger = logging.getLogger(__name__)

# 定义模板不做选择：只有一份默认模板，模型不点名。
DEFAULT_TEMPLATE = "generic"

# 空在途却以「已经在办」放弃：程序拦一次，逼模型重判。
_FALSE_IN_FLIGHT_MARKERS = ("已经在办", "还在办", "正在办", "已经在处理")

PlanSkillOutput = Union[PlanResult, LookupAsk]


def _looks_like_in_flight_refusal(error: str) -> bool:
    text = (error or "").strip()
    return any(marker in text for marker in _FALSE_IN_FLIGHT_MARKERS)


def _merge_observed_estimate(
    estimate: Mapping[str, Any], observed: Mapping[str, Any]
) -> dict[str, Any]:
    """模型没给估价时，用 tool.json 里的实测均值补齐。"""
    merged = dict(estimate)
    if merged.get("time_est_ms") is None:
        avg = observed.get("avg_time_ms")
        if isinstance(avg, (int, float)) and not isinstance(avg, bool):
            merged["time_est_ms"] = int(round(float(avg)))
    if merged.get("cost_est") is None:
        avg = observed.get("avg_cost")
        if isinstance(avg, (int, float)) and not isinstance(avg, bool):
            merged["cost_est"] = float(avg)
    runs = observed.get("runs")
    if isinstance(runs, int) or (
        isinstance(runs, float) and not isinstance(runs, bool)
    ):
        merged["observed_runs"] = int(runs)
    return merged


TOOL_PLAN_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "command": {"type": "string"},
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
        "ask": {"enum": ["execute", "propose_only", "create_tool"]},
        "error": {"type": "string"},
        "create_tool": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "tool_intent": {"type": "string"},
                "params_schema": {"type": "object"},
                "expected_output": {"type": "string"},
                "cost_estimate": {"type": ["number", "null"]},
                "feedback_plan": {
                    "type": "object",
                    "properties": {
                        "stages": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "result_fields": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        },
        "plan": {
            "type": "object",
            "properties": {
                "mode": {
                    "enum": [
                        "sequential",
                        "parallel",
                        "mixed",
                        "agent_loop",
                    ]
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_id": {"type": "string"},
                            "command": {"type": "string"},
                            "params": {"type": "object"},
                            "expected_result": {"type": "string"},
                            "ask": {"enum": ["execute", "create_tool"]},
                            "create_tool": {
                                "type": "object",
                                "properties": {
                                    "tool_name": {"type": "string"},
                                    "tool_intent": {"type": "string"},
                                    "params_schema": {"type": "object"},
                                    "expected_output": {"type": "string"},
                                    "cost_estimate": {
                                        "type": ["number", "null"]
                                    },
                                    "feedback_plan": {"type": "object"},
                                },
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "lookup": {
            "type": "object",
            "properties": {
                "op": {"enum": ["search", "describe"]},
                "query": {"type": "string"},
                "queries": {"type": "array", "items": {"type": "string"}},
                "name": {"type": "string"},
                "names": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            },
        },
    },
    "required": ["ok"],
}


class ToolPlanSkill(Skill[PlanSkillOutput]):
    name = "tool_plan"
    version = "v2"
    schema = TOOL_PLAN_SCHEMA
    instruction = """你是匠石的工具策划，不是对说话的那一轮认知。工具要不要用，上一环已经决定；你只负责把这次 need 变成一条可执行的工具定义，或给出放弃理由。

只输出一个 JSON 对象，不要任何解释、注释或 Markdown。

【输入】
- 本轮任务 JSON：need、verbal、object_id、field_ref、scene、catalog、engine_tools 或 discovery、budget，以及 in_flight。
- 只看本轮 need、现场和目录/命中；片场里旧的失败、不相干负面，不要拿来判定这次。
- in_flight：该说话对象当前全部未终态记挂（按对象，不按本轮活动）。每项含 task_id / phase / tool_name / purpose / need / progress。phase 为 use（正在执行一次查询或动作）、create（正在造工具）或 propose（提案未执行）。tool_name 是命令名或正在造的工具名。purpose 是该工具的能力说明，只用来识别「在造哪一种工具」，不用来判断「本轮查询是否属于该能力域」。progress 是人类可读状态备注，不参与判定。
- **优先检查 in_flight**。空列表不得以「已经在办」放弃；已终态不计入。按 phase 分别判定是否重复开工，不要用 purpose 覆盖本轮 need：
  - phase=use：只拦「同一查询还在跑」。比本轮 need 与该项 need（可辅以相同 tool_name）。地点、币种、对象等关键参数不同，不是同一查询，不产生约束，可以再 execute。
  - phase=create 或 propose：拦两件事。其一，不要再 create / propose 同一工具：本轮若是造工具，且 tool_name 相同，或 purpose 表明正在造的就是本轮也要造的那一种能力，则 ok=false。其二，本轮若是查询、且显然只能靠正在造的那一本才能做（目录里还没有替代），不要再造一本，也不要假装 execute；ok=false，说明工具还在造。天气查询对上正在造的汇率工具，不产生约束。
  - 命中时 error 写「我这边已经在办…」或「我这边还在造这个工具…」，不再 execute，也不再为此重复 create。
- discovery 模式后续轮次会提供 lookups（lookup.search/describe 的命中结果），必须读取 lookups。
- 判定范围：仅依据本轮 need、in_flight、scene、catalog / lookups；片场中旧失败记录、无关负面信息，不纳入本次判定。
- scene 仅用于把缺失的对象、地点、产物补充进 params；不修改需求本身，不生成对外话术。

【三种结局，三选一】
1. 目录存在能力匹配的现成工具：
   ask=execute（或 propose_only），command 填写目录内真实工具名，params 仅填入工具声明支持的参数。
2. 目录无匹配工具，但 need 清晰：
   ask=create_tool，command 置空，在 create_tool 填写完整规格。
   - 这是正常路径，不是失败：need 清晰、风险可控、预算允许，则创建工具。
3. 满足任一条件，ok=false 放弃：
   - need 本身模糊，缺少关键信息；
   - 风险明显不可接受；
   - 预算明确不足；
   - in_flight 按上面规则构成重复开工，或查询所依赖的工具仍在造。
   ok=false 时，error 以「我 / 我这边」开头写一句原因；不发起任何动作。目录缺少工具本身不等于放弃，除非同时需求不清。
   budget 当前为无限占位，暂不会因预算不足放弃；预算规则保留，接入真实预算后自动生效。

【多工具计划】
- 如果 need 明显需要多个工具步骤，输出 plan，而不是单条 command。
- mode 选择：
  - parallel：步骤之间没有依赖，可以同时做。
  - sequential：步骤之间有明确先后。
  - mixed：部分步骤并行、部分步骤有先后。
  - agent_loop：后面步骤依赖前面结果，或需要 Pi 根据中间结果动态决定下一步。
- 每个 step 必须有唯一 step_id；depends_on 只能引用本 plan 里已有的 step_id。
- 相互独立的步骤不要互相写 depends_on，程序会同时启动所有就绪步骤。
- 每个 step 仍遵守 command / params / expected_result / create_tool 的规则。
- agent_loop 只给总目标和候选步骤，不替 Pi 固定每一步的内部判断。
- 只拆必要步骤，不做无意义拆分。

【目录与 lookup】
- engine_tools：名单完整，直接选 command。
- discovery：名单不完整，可 lookup.search 或 lookup.describe。search 用功能词，不要整句 need、人名或地点；queries 最多包含 5 个功能关键词，后端合并返回 top-K 候选。lookup 只查元信息，不执行工具。
- 检索得到的候选仍不合适时，进入 create 或放弃，而不是硬选近似工具。

【填写】
- command：只用目录里真实存在的名字；不要编造，也不要把模板名当命令名。
- params：只填工具声明支持的键。
- expected_result：一句说清成功后的产出；写不出就空。
- estimate：先看所选命令的 meta.observed。有 runs 和 avg_time_ms / avg_cost / avg_tokens 时，把 avg_time_ms 填进 time_est_ms，把 avg_cost 填进 cost_est；没有 observed 才用目录说明或模板槽位；仍没有依据时时间、费用填 null，benefit/downside 空，need_confirm false。
- create_tool：tool_name 建议名、tool_intent 能力意图、params_schema 参数 JSON Schema、expected_output 预期产出、cost_estimate 预估开销、feedback_plan 反馈方案（stages 阶段名、result_fields 结果字段）；非 create_tool 时为空对象。
- error：ok=false 时才有，像匠石自己的认知，有主语；不要写字段名、变量名、JSON 键名，也不要照抄工具清单原文，字符串内不换行。例：「我手上现在没有能查天气的工具」。
- 外部已经向用户说过需求的，定义要和那句一致，不另起需求。"""

    def parse(self, data: Mapping[str, Any]) -> PlanSkillOutput:
        lookup = parse_lookup(data)
        if lookup is not None:
            return lookup
        if not bool(data.get("ok")):
            error = str(data.get("error") or "").strip() or "策划失败"
            return PlanFailure(error)
        plan_data = data.get("plan")
        if isinstance(plan_data, Mapping) and isinstance(
            plan_data.get("steps"), Sequence
        ):
            steps = tuple(
                self._parse_step(step)
                for step in plan_data.get("steps", ())
                if isinstance(step, Mapping)
            )
            mode = str(plan_data.get("mode") or "sequential").strip()
            if mode not in {"sequential", "parallel", "mixed", "agent_loop"}:
                mode = "sequential"
            return ToolPlan(
                plan_id=new_id(),
                need="",
                mode=mode,
                steps=steps,
            )
        params = data.get("params") if isinstance(data.get("params"), Mapping) else {}
        raw_est = data.get("estimate") if isinstance(data.get("estimate"), Mapping) else {}
        ask_raw = str(data.get("ask") or AskMode.EXECUTE.value).strip()
        try:
            ask = AskMode(ask_raw)
        except ValueError:
            ask = AskMode.EXECUTE
        return ToolRequest(
            template=DEFAULT_TEMPLATE,
            command=str(data.get("command") or "").strip(),
            create=self._parse_create(data, ask),
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

    @staticmethod
    def _parse_create(
        data: Mapping[str, Any], ask: AskMode
    ) -> CreateToolSpec | None:
        """读模型给的造工具规格。没给就返回 None。"""
        if ask is not AskMode.CREATE_TOOL:
            return None
        raw = data.get("create_tool")
        if not isinstance(raw, Mapping):
            return None
        schema_raw = raw.get("params_schema")
        cost_raw = raw.get("cost_estimate")
        cost = (
            float(cost_raw)
            if isinstance(cost_raw, (int, float)) and not isinstance(cost_raw, bool)
            else None
        )
        feedback_raw = raw.get("feedback_plan")
        return CreateToolSpec(
            # 落账前就规范化，保证账本 / 引擎 / 实测统计三处同名。
            tool_name=normalize_tool_name(str(raw.get("tool_name") or "")),
            tool_intent=str(raw.get("tool_intent") or "").strip(),
            params_schema=dict(schema_raw) if isinstance(schema_raw, Mapping) else {},
            expected_output=str(raw.get("expected_output") or "").strip(),
            cost_estimate=cost,
            feedback_plan=dict(feedback_raw)
            if isinstance(feedback_raw, Mapping)
            else {},
        )

    @classmethod
    def _parse_step(cls, step: Mapping[str, Any]) -> ToolStep:
        ask_raw = str(step.get("ask") or AskMode.EXECUTE.value).strip()
        try:
            ask = AskMode(ask_raw)
        except ValueError:
            ask = AskMode.EXECUTE
        params = step.get("params") if isinstance(step.get("params"), Mapping) else {}
        depends_on = step.get("depends_on")
        depends = tuple(
            str(item).strip()
            for item in depends_on
            if str(item or "").strip()
        ) if isinstance(depends_on, Sequence) and not isinstance(depends_on, (str, bytes)) else ()
        raw_group = step.get("parallel_group")
        try:
            parallel_group = int(raw_group) if raw_group is not None else 0
        except (TypeError, ValueError):
            parallel_group = 0
        return ToolStep(
            step_id=str(step.get("step_id") or "").strip() or new_id(),
            request=ToolRequest(
                template=DEFAULT_TEMPLATE,
                command=str(step.get("command") or "").strip(),
                create=cls._parse_create(step, ask),
                params=dict(params),
                expected_result=str(step.get("expected_result") or "").strip(),
                ask=ask,
            ),
            depends_on=depends,
            parallel_group=parallel_group,
        )

    def _fallback(self, raw_text: str) -> PlanSkillOutput:
        del raw_text
        return PlanFailure("我这边没能定下该怎么办。")

    def plan_intake(
        self,
        intake: IntakeRecord,
        catalog: Sequence[Mapping[str, Any]],
        engine_tools: Sequence[Mapping[str, Any]] = (),
        scene: str = "",
        in_flight: Sequence[Mapping[str, Any]] = (),
    ) -> PlanResult:
        index = ToolIndex.from_maps(engine_tools)
        engine_names = index.names
        lookups: list[dict[str, Any]] = []
        planned: PlanSkillOutput | None = None
        forced_note = ""
        for round_i in range(LOOKUP_MAX_ROUNDS + 1):
            payload = self._task_payload(
                intake, catalog, index, scene, lookups, in_flight=in_flight
            )
            if forced_note:
                payload = {**payload, "note": forced_note}
            request: ModelRequest = tool_skill_request(
                intake.subject_id,
                payload,
            )
            planned = self.run(request)
            if isinstance(planned, LookupAsk):
                if round_i >= LOOKUP_MAX_ROUNDS:
                    return PlanFailure("我这边还没能从目录里定下该用哪个工具。")
                lookups.append(index.dispatch(planned))
                continue
            if (
                isinstance(planned, PlanFailure)
                and not in_flight
                and not forced_note
                and _looks_like_in_flight_refusal(planned.message)
            ):
                # in_flight 为空却报已经在办：多为把已结束记挂当在途。重判一次。
                forced_note = (
                    "in_flight 当前为空，不得以已经在办为由放弃；"
                    "若 need 仍值得做，请 execute 或 create。"
                )
                continue
            break
        if planned is None:
            return PlanFailure("我这边没能定下该怎么办。")
        if isinstance(planned, LookupAsk):
            return PlanFailure("我这边还没能从目录里定下该用哪个工具。")
        if isinstance(planned, PlanFailure):
            if (
                not in_flight
                and _looks_like_in_flight_refusal(planned.message)
            ):
                return PlanFailure(
                    "我这边刚才误判成已经在办了；当前没有在途记挂，请换个说法说明真正原因，或直接执行。"
                )
            return planned
        if isinstance(planned, ToolPlan):
            return self._finalize_plan(
                intake,
                planned,
                engine_names,
                index,
            )
        # 三种结局，没有第四种：复用现成命令 / 造一个新工具 / 不做。
        # 这里**不再**替模型兜底填那条唯一的命令——那是旧程序的破法：
        # 目录里只有一条时，模型说「我要造」，程序会拿那条唯一的命令去执行。
        command = (planned.command or "").strip()
        create_spec = planned.create
        wants_create = create_spec is not None or planned.ask is AskMode.CREATE_TOOL
        if wants_create:
            if create_spec is None:
                return PlanFailure("我这边没有能完成该需求的工具，也没说清该造个什么样的。")
            if command:
                return PlanFailure("我这边没能定下是造一个新工具还是用现成的。")
        else:
            if not command:
                return PlanFailure("我这边没能定下该用哪个工具。")
            if engine_names and command not in engine_names:
                return PlanFailure("我这边没能定下该用哪个工具，引擎目录里没有这一条。")
        origin = ToolOrigin.EXTERNAL_05
        raw_origin = (intake.origin or "").strip()
        if raw_origin:
            try:
                origin = ToolOrigin(raw_origin)
            except ValueError:
                origin = ToolOrigin.EXTERNAL_05
        estimate = dict(
            planned.meta.get("estimate")
            if isinstance(planned.meta, Mapping)
            else {}
        )
        if command:
            estimate = _merge_observed_estimate(estimate, index.observed(command))
        return ToolRequest(
            subject_id=intake.subject_id,
            activity_id=intake.activity_id,
            origin=origin,
            need=intake.need,
            template=DEFAULT_TEMPLATE,
            command=command,
            create=create_spec,
            params=planned.params,
            expected_result=planned.expected_result,
            ask=planned.ask,
            budget=MAX_BUDGET,
            meta={"estimate": dict(estimate or {})},
        )

    def _finalize_plan(
        self,
        intake: IntakeRecord,
        plan: ToolPlan,
        engine_names: set[str],
        index: ToolIndex,
    ) -> PlanResult:
        step_ids = {step.step_id for step in plan.steps}
        if len(step_ids) != len(plan.steps):
            return PlanFailure("我这边没能定下计划，步骤编号重复。")
        origin = ToolOrigin.EXTERNAL_05
        raw_origin = (intake.origin or "").strip()
        if raw_origin:
            try:
                origin = ToolOrigin(raw_origin)
            except ValueError:
                origin = ToolOrigin.EXTERNAL_05
        final_steps: list[ToolStep] = []
        for step in plan.steps:
            if not step.step_id:
                return PlanFailure("我这边没能定下计划，步骤缺少编号。")
            for dep in step.depends_on:
                if dep not in step_ids:
                    return PlanFailure("我这边没能定下计划，步骤依赖不存在。")
            request = step.request
            create_spec = request.create
            wants_create = create_spec is not None or request.ask is AskMode.CREATE_TOOL
            if wants_create:
                if create_spec is None:
                    return PlanFailure(
                        "我这边没能定下计划，有一步要造工具但没说清造什么。"
                    )
                if request.command:
                    return PlanFailure(
                        "我这边没能定下计划，造工具步骤不能同时点名现成工具。"
                    )
            else:
                command = (request.command or "").strip()
                if not command:
                    return PlanFailure("我这边没能定下计划，有一步没有指定工具。")
                if engine_names and command not in engine_names:
                    return PlanFailure(
                        "我这边没能定下计划，工具目录里没有这一步要用的工具。"
                    )
            raw_estimate = (
                request.meta.get("estimate")
                if isinstance(request.meta, Mapping)
                else {}
            )
            estimate = dict(raw_estimate) if isinstance(raw_estimate, Mapping) else {}
            if request.command:
                estimate = _merge_observed_estimate(
                    estimate, index.observed(request.command)
                )
            bound = replace(
                request,
                subject_id=intake.subject_id,
                activity_id=intake.activity_id,
                origin=origin,
                need=intake.need,
                template=DEFAULT_TEMPLATE,
                budget=MAX_BUDGET,
                meta={"estimate": estimate},
            )
            final_steps.append(
                ToolStep(
                    step_id=step.step_id,
                    request=bound,
                    depends_on=step.depends_on,
                    parallel_group=step.parallel_group,
                )
            )
        return ToolPlan(
            plan_id=plan.plan_id,
            need=intake.need,
            mode=plan.mode,
            steps=tuple(final_steps),
        )

    def _task_payload(
        self,
        intake: IntakeRecord,
        catalog: Sequence[Mapping[str, Any]],
        index: ToolIndex,
        scene: str,
        lookups: Sequence[Mapping[str, Any]],
        in_flight: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "need": intake.need,
            "verbal": intake.verbal,
            "object_id": intake.object_id,
            "field_ref": dict(intake.field_ref),
            "scene": scene,
            "catalog": list(catalog),
            "budget": budget_to_dict(MAX_BUDGET),
            "in_flight": [dict(item) for item in in_flight],
        }
        if index.is_eager:
            payload["engine_tools"] = index.eager_tools()
        else:
            payload["discovery"] = index.disclose().as_dict()
            payload["lookups"] = [dict(item) for item in lookups]
        return payload


class SkillPlanner:
    """把 ToolPlanSkill 接到 ToolPlanner 口。测里仍可注入 RulePlanner。"""

    def __init__(
        self,
        skill: ToolPlanSkill,
        catalog: Sequence[Mapping[str, Any]] | None = None,
        engine_tools: Sequence[Mapping[str, Any]] | None = None,
        engine: Any = None,
        scene_loader: Callable[[IntakeRecord], str] | None = None,
        hang_store: Any = None,
    ) -> None:
        self.skill = skill
        self.catalog = tuple(catalog or ())
        self.engine = engine
        # 注入进来的目录不刷新；从引擎读来的要能在造完工具之后失效。
        self._tools_fixed = engine_tools is not None
        self.engine_tools = tuple(engine_tools or ())
        self._tools_read = self._tools_fixed
        self.scene_loader = scene_loader
        self.hang_store = hang_store

    def invalidate_tools(self) -> None:
        """造出新工具之后调用：下一次策划重新读引擎目录。

        不失效的话，同一会话里造出来的工具检索不到，于是会重复造。
        这是《205 工具检索》§8.5 记下的第一个硬前置。
        """
        if self._tools_fixed:
            return
        self._tools_read = False
        self.engine_tools = ()

    def _resolve_tools(self) -> tuple[Mapping[str, Any], ...]:
        if self._tools_read:
            return self.engine_tools
        if self.engine is None:
            return ()
        from jshi.tool.catalog import engine_tools as read_engine_tools

        self.engine_tools = read_engine_tools(self.engine)
        self._tools_read = True
        return self.engine_tools

    def _engine_error(self) -> str:
        """只认「起不来 / 列目录失败」，不认上一次执行的 timeout/cancelled。

        否则造工具跑满墙钟后 ``_last_stop`` 仍是 timeout，下一轮策划会被误判成
        「连不上工具引擎」，明明 Pi 还活着。
        """
        if self.engine is None:
            return ""
        return str(getattr(self.engine, "_spawn_error", "") or "").strip()

    def _load_scene(self, intake: IntakeRecord) -> str:
        if self.scene_loader is None:
            return ""
        try:
            return (self.scene_loader(intake) or "").strip()
        except Exception:
            logger.exception("205 现场加载失败")
            return ""

    def _tool_purpose(self, hang: Any, command: str) -> str:
        meta = hang.meta if isinstance(getattr(hang, "meta", None), Mapping) else {}
        intent = str(meta.get("tool_intent") or "").strip()
        if intent:
            return intent
        expected = str(meta.get("expected_output") or "").strip()
        if expected:
            return expected
        key = (command or "").strip()
        if not key:
            return ""
        for item in self.engine_tools:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "").strip()
            if name != key:
                continue
            desc = str(item.get("description") or "").strip()
            if desc:
                return desc
        return ""

    def _in_flight(self, intake: IntakeRecord) -> tuple[dict[str, Any], ...]:
        store = self.hang_store
        if store is None:
            return ()
        try:
            opens = store.list_open(intake.subject_id, intake.object_id)
        except Exception:
            logger.exception("205 读取在途记挂失败")
            return ()
        items: list[dict[str, Any]] = []
        for hang in opens:
            phase = (hang.kind or "use").strip() or "use"
            tool_name = (hang.command or "").strip()
            items.append(
                {
                    "task_id": hang.task_id,
                    "phase": phase,
                    "tool_name": tool_name,
                    "purpose": self._tool_purpose(hang, tool_name),
                    "need": hang.need,
                    "progress": "running",
                }
            )
        return tuple(items)

    def plan(self, intake: IntakeRecord) -> PlanResult:
        tools = self._resolve_tools()
        engine_error = self._engine_error()
        if engine_error:
            return PlanFailure("我这边现在连不上工具引擎，暂时没法判断或执行。")
        return self.skill.plan_intake(
            intake,
            self.catalog,
            tools,
            scene=self._load_scene(intake),
            in_flight=self._in_flight(intake),
        )
