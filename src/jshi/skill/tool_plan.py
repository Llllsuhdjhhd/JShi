"""205 策划 skill：选模板、填参数、写估价；大目录时先检索再点名。"""

from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
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
        "waiting_for": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 200}},
        "create_tool": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "tool_intent": {"type": "string"},
                "params_schema": {"type": "object"},
                "expected_output": {"type": "string"},
                "cost_estimate": {"type": ["number", "null"]},
                "fulfill_after_create": {"type": "boolean"},
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
                            "need": {"type": "string"},
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
                                    "fulfill_after_create": {"type": "boolean"},
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
TOOL_PLAN_SCHEMA["properties"]["requirements"] = {"type": "array", "maxItems": 16, "items": {"type": "string"}}
from jshi.tool.recording import TOOL as RECORDING_TOOL, COMMAND as RECORDING_COMMAND
TOOL_PLAN_SCHEMA['properties']['task_type'] = {'enum': ['general', 'dialogue_recording']}
TOOL_PLAN_SCHEMA['properties']['dialogue_recording'] = RECORDING_TOOL['params']
TOOL_PLAN_SCHEMA['required'].append('task_type')


class ToolPlanSkill(Skill[PlanSkillOutput]):
    name = "tool_plan"
    version = "v2"
    schema = TOOL_PLAN_SCHEMA
    instruction = """你是匠石的工具策划，不是对说话的那一轮认知。工具要不要用，上一环已经决定；你只负责把这次 need 变成一条可执行的工具定义，或给出放弃理由。

只输出一个 JSON 对象，不要任何解释、注释或 Markdown。

【输入】
- 本轮任务 JSON：need、verbal、scene、catalog、engine_tools 或 discovery、budget、execution_capabilities，以及 in_flight；均已按当前事项所属人物筛选，不带内部人物或现场长ID。
- tool_materials（若有）：已取得的工具结果与事项关系。先看已有材料，只策划缺少或明确要求更新的部分；失败、过期、不完整的结果不能当成已经满足需求。
- work_terms（若有）：原事项需求、补全后的需求、待补项和适用截止；now 是带时区的当前时间。不要把原请求的相对日期改成重试当天；过期不执行。补信息后保留已完成的步骤，只补剩余部分。
- user_input（若有）是本次用户原话，verbal 是匠石已说的回应；不能把回应里的承诺当成已执行事实。原话含多项要求时逐项核对 need 的范围，遗漏或无法执行的部分明确报告，不只完成第一项就宣称全成。
- recent_user_utterances是宿主保留的该人物近期原话，辅助还原连续委托；与need缩写冲突时不能漏掉原话中的持续要求。它不是完整聊天记录，不供执行器据此编造对话。
- work_terms.recording_goal是此前已接受的持续记录目标，独立于单次执行是否成功；continuous=true时补录沿用原文件和持续范围，不能用export或文件结束标记将其结束。只有明确停止、取消或约定结束才收尾；工具回执中的running/blocked/stopped才是实际运行状态。
- “补进去”不撤销原有持续记录要求。文件结束标记、旧工具输出、匠石回应或need里自行增加的“只办一次”都不能代替用户停止指令；收窄目标必须有用户原话或原约定结束条件依据。既往持续目标存在时用start并按history/since补录，不能仅export；现有证据无法判断范围时按澄清规则询问。
- task_type必须区分general与dialogue_recording。凡目标是保存、补录或检查本系统的实际对话原文，选dialogue_recording，并在dialogue_recording填宿主操作参数；不要输出plan、create_tool或让Pi自己生成聊天正文。宿主有真实输入与回复交付源，Pi只有任务描述，两者不能互相替代。start持续记录直到停止，export只导出当前已保留原话，status只核验，stop结束，resume恢复受阻任务。默认记录双方include_replies=true；只要人的话才false。补录近期内容用history=available，指定时间用带时区的since；缺失历史如实报告。原委托包含“后续也记录”时必须保留start，不能缩成export；仅要求导出已有记录则export。范围不清按澄清规则询问。持续任务只有查到宿主任务running才算已启动；文件存在或Pi成功均不证明启动。
- requirements逐项列出本次必须满足的可核验要求，最多16项；完整保留用户补充和持续执行、停止、交付要求。每项不能只是“执行成功”。依赖中间结果调整方法的任务优先agent_loop，交由Pi完成并接受监督纠偏。
- 先核对完整目标、必须满足的要求和完成条件，再拆步骤。一次执行、等待外部信息与持续执行分别规划；持续任务必须有真实输入来源、后续触发、进度保存和停止机制。future_dialogue_input=true时，持续记录对话必须使用runtime:dialogue-recording；它由宿主接收未来输入并落盘，Pi本身不会持续接收对话。此命令放在单独工具请求或sequential步骤中，不能放入Pi的agent_loop；启动后保持事项开放直到停止收尾。默认只记录委托人，明确要求记录所有人时scope=all。future_dialogue_input=false时不可用一次写入冒充持续采集，应报告能力缺口。
- 目标本身、执行范围、交付内容、验收标准或必需参数仍有影响执行的歧义时，先核对原话、work_terms中的既往问答和现场材料；仍无法确定则ok=false、waiting_for列最多5个具体问题、error简述为何影响执行。主认知会向原委托人询问，回答会带着同一事项的原始要求、历次补充和待补项回来；允许多轮澄清，直到足以执行再规划。工具侧不直接对用户说话，不擅自替用户确定目标，也不反复问已回答的问题。清楚且可执行时waiting_for留空，不为普通实施选择增加确认。能力不足应明确报告，不能转成无意义的询问。
- 综合本轮need、用户原话、work_terms、现场和目录/命中；片场里无关的旧失败不用于否定本次能力。
- in_flight：该说话对象当前全部未终态记挂（按对象，不按本轮活动）。每项含 phase / tool_name / purpose / need / progress。phase 为 use（正在执行一次查询或动作）、create（正在造工具）或 propose（提案未执行）。tool_name 是命令名或正在造的工具名。purpose 是该工具的能力说明，只用来识别「在造哪一种工具」，不用来判断「本轮查询是否属于该能力域」。progress 是人类可读状态备注，不参与判定。
- **优先检查 in_flight**。空列表不得以「已经在办」放弃；已终态不计入。按 phase 分别判定是否重复开工，不要用 purpose 覆盖本轮 need：
  - phase=use：只拦「同一查询还在跑」。比本轮 need 与该项 need（可辅以相同 tool_name）。地点、币种、对象等关键参数不同，不是同一查询，不产生约束，可以再 execute。
  - phase=create 或 propose：拦两件事。其一，不要再 create / propose 同一工具：本轮若是造工具，且 tool_name 相同，或 purpose 表明正在造的就是本轮也要造的那一种能力，则 ok=false。其二，本轮若是查询、且显然只能靠正在造的那一本才能做（目录里还没有替代），不要再造一本，也不要假装 execute；ok=false，说明工具还在造。天气查询对上正在造的汇率工具，不产生约束。
  - 命中时 error 写「我这边已经在办…」或「我这边还在造这个工具…」，不再 execute，也不再为此重复 create。
- discovery 模式后续轮次会提供 lookups（lookup.search/describe 的命中结果），必须读取 lookups。
- 判定范围：本轮need、user_input、recent_user_utterances、work_terms、in_flight、scene及catalog / lookups；旧结果的时效按当前核验材料理解，不沿用已经过时的“未写入”等结论。
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
    - execution_capabilities 明确不支持完成目标所需的执行方式，不能靠创建单次工具补足；
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
- 每步 need 只描述这一步做什么，expected_result 写这一步的验收条件；总目标仅作背景。核验步骤只读已有产物，不重新创建、覆盖或修补；发现缺失或不一致交回失败证据，由后续明确的修复步骤处理。
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
- create_tool：tool_name 建议名、tool_intent 能力意图、params_schema 参数 JSON Schema、expected_output 预期产出、cost_estimate 预估开销、feedback_plan 反馈方案（stages 阶段名、result_fields 结果字段）。fulfill_after_create：原需求是查找/执行任务时为 true，造好后后台接着用；原需求只要求造工具时为 false。非 create_tool 时为空对象。
- error：ok=false 时才有，像匠石自己的认知，有主语；不要写字段名、变量名、JSON 键名，也不要照抄工具清单原文，字符串内不换行。例：「我手上现在没有能查天气的工具」。
- 外部已经向用户说过需求的，定义要和那句一致，不另起需求。"""

    def parse(self, data: Mapping[str, Any]) -> PlanSkillOutput:
        lookup = parse_lookup(data)
        if lookup is not None:
            return lookup
        if not bool(data.get("ok")):
            error = str(data.get("error") or "").strip() or "策划失败"
            from jshi.tool.validity import pending_information
            return PlanFailure(error, pending_information(data.get('waiting_for')))
        recording = data.get('dialogue_recording')
        if data.get('task_type') == 'dialogue_recording' or isinstance(recording, Mapping):
            if not isinstance(recording, Mapping) or not recording.get('action'):
                return PlanFailure('我这边还没有确定记录操作，未执行。')
            if data.get('plan') or data.get('create_tool'):
                return PlanFailure('我这边的记录计划混入了其他执行方式，未执行。')
            return ToolRequest(template=DEFAULT_TEMPLATE, command=RECORDING_COMMAND,
                params=dict(recording), ask=AskMode.EXECUTE,
                meta={'requirements': tuple(str(x) for x in data.get('requirements', ()))[:16]})
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
                requirements=tuple(str(x).strip() for x in data.get('requirements', ()) if str(x).strip())[:16],
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
                "requirements": tuple(str(x).strip() for x in data.get('requirements', ()) if str(x).strip())[:16],
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
            fulfill_after_create=raw.get("fulfill_after_create") is not False,
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
                need=str(step.get("need") or "").strip(),
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

    def plan_intake(self, intake, catalog, engine_tools=(), scene='', in_flight=(), materials=''):
        result = self._plan_intake_once(intake, catalog, engine_tools, scene, in_flight, materials)
        repairable = ('没有指定工具', '没能定下该用哪个工具', '步骤缺少编号', '步骤编号重复',
                      '步骤依赖不存在', '互相依赖', '记录计划混入', '没有确定记录操作')
        if isinstance(result, PlanFailure) and not result.waiting_for and any(t in result.message for t in repairable):
            intake = replace(intake, meta={**intake.meta, 'planning_error': result.message})
            return self._plan_intake_once(intake, catalog, engine_tools, scene, in_flight, materials)
        return result

    def _plan_intake_once(
        self,
        intake: IntakeRecord,
        catalog: Sequence[Mapping[str, Any]],
        engine_tools: Sequence[Mapping[str, Any]] = (),
        scene: str = "",
        in_flight: Sequence[Mapping[str, Any]] = (),
        materials: str = "",
    ) -> PlanResult:
        index = ToolIndex.from_maps(engine_tools)
        engine_names = index.names
        lookups: list[dict[str, Any]] = []
        planned: PlanSkillOutput | None = None
        forced_note = ""
        for round_i in range(LOOKUP_MAX_ROUNDS + 1):
            payload = self._task_payload(
                intake, catalog, index, scene, lookups, in_flight=in_flight, materials=materials
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
                and not planned.waiting_for
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
                not in_flight and not planned.waiting_for
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
            if command == RECORDING_COMMAND and command not in engine_names:
                return PlanFailure('我这边没有接入真实对话记录源，未执行记录。')
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
            planned.meta.get("estimate") or {}
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
            meta={
                "requirements": planned.meta.get('requirements') or (planned.expected_result or intake.need,),
                "estimate": dict(estimate or {}),
                "feedback_plan": dict(
                    create_spec.feedback_plan
                    if create_spec is not None
                    else index.feedback_plan(command)
                ),
            },
        )

    def _finalize_plan(
        self,
        intake: IntakeRecord,
        plan: ToolPlan,
        engine_names: set[str],
        index: ToolIndex,
    ) -> PlanResult:
        step_ids = {step.step_id for step in plan.steps}
        if plan.mode == 'agent_loop' and any((step.request.command or '').startswith('runtime:') for step in plan.steps):
            return PlanFailure('持续记录由宿主执行，请使用独立请求或顺序计划，不能放入Pi会话。')
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
                need=request.need or request.expected_result or "按本步参数执行指定工具，返回实际结果。",
                template=DEFAULT_TEMPLATE,
                budget=MAX_BUDGET,
                meta={
                    "overall_need": intake.need,
                    "step_scope": True,
                    "requirements": (request.expected_result or request.need or intake.need,),
                    "goal_requirements": plan.requirements,
                    "estimate": estimate,
                    "feedback_plan": dict(
                        create_spec.feedback_plan
                        if create_spec is not None
                        else index.feedback_plan(request.command)
                    ),
                },
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
            requirements=plan.requirements,
        )

    def _task_payload(
        self,
        intake: IntakeRecord,
        catalog: Sequence[Mapping[str, Any]],
        index: ToolIndex,
        scene: str,
        lookups: Sequence[Mapping[str, Any]],
        in_flight: Sequence[Mapping[str, Any]] = (),
        materials: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "need": intake.need,
            "verbal": intake.verbal,
            "scene": scene,
            "catalog": list(catalog),
            "budget": budget_to_dict(MAX_BUDGET),
            "in_flight": [{k:v for k,v in item.items() if k != 'task_id'} for item in in_flight],
            "execution_capabilities": {"future_dialogue_input": 'runtime:dialogue-recording' in index.names},
        }
        if materials:
            payload["tool_materials"] = materials
        if intake.meta.get('user_input'):
            payload['user_input'] = intake.meta['user_input']
        if intake.meta.get('recent_user_utterances'):
            payload['recent_user_utterances'] = json.loads(intake.meta['recent_user_utterances'])
        if intake.meta.get('planning_error'):
            payload['planning_error'] = '上次计划未执行，结构校验未通过：' + intake.meta['planning_error'] + '。修正执行定义后重新输出，不能改写用户目标。'
        # Runtime capabilities are always visible, even when the Pi catalog is large.
        if RECORDING_COMMAND in index.names:
            payload['runtime_tools'] = [RECORDING_TOOL]
        payload['now'] = datetime.now(timezone.utc).isoformat(timespec='minutes')
        if intake.meta.get('work_terms'):
            payload['work_terms'] = json.loads(intake.meta['work_terms'])
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
        material_loader: Callable[[IntakeRecord], str] | None = None,
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
        self.material_loader = material_loader
        self.extra_tools = ()

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
        tools = (*self._resolve_tools(), *self.extra_tools)
        engine_error = self._engine_error()
        if engine_error and not self.extra_tools:
            return PlanFailure("我这边现在连不上工具引擎，暂时没法判断或执行。")
        if engine_error:
            tools = self.extra_tools
        scene = self._load_scene(intake)
        materials = ""
        if self.material_loader is not None:
            try:
                materials = self.material_loader(intake)
            except Exception:
                logger.exception("205 已有工具材料加载失败")
        kwargs = {"scene": scene, "in_flight": self._in_flight(intake)}
        if materials:
            kwargs["materials"] = materials
        result = self.skill.plan_intake(intake, self.catalog, tools, **kwargs)
        if engine_error:
            requests = [s.request for s in result.steps] if isinstance(result, ToolPlan) else [result] if isinstance(result, ToolRequest) else []
            if any(r.create is not None or not (r.command or '').startswith('runtime:') for r in requests):
                return PlanFailure('Pi暂不可用，当前只能执行已提供的宿主记录操作。')
        return result
