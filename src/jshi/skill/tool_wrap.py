"""200 包装 skill：按阶段选择三种提示词，把阶段事实写成给 03 的一句。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from jshi.models import ModelRequest
from jshi.tool.contract import FeedbackKind, ToolFeedback, ToolStatus
from jshi.tool.hang import HangRecord

from .base import Skill, parse_json_object
from .tool_io import tool_skill_request

TOOL_WRAP_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "visible": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": ["visible"],
}

_WRAP_STYLE = """【写法】
summary 必须是功能向、描述性的阶段性事实：正在做什么、做到哪一步、结果是什么。
读到这句话的人要用它判断「事办到哪了 / 答案是什么」，不是要看技术细节。
禁止写入：token、耗时毫秒、费用数字、路径、文件名、命令行、JSON 键名、变量名、
堆栈、引擎内部阶段名、原始日志大段照抄。输入里出现这些也要丢掉，改写成白话。
输入 text / result_text 往往很杂，只抽取对 need 有用的事实或进展；抽不出则 visible=false。"""

PLANNING_PROMPT = """你是匠石的工具反馈包装器。你只负责把「工具策划」这一阶段的结果，写成一句给匠石自己看的事实。
不是对用户开口，不要称呼对方，不要写成 response_plan。
只输出一个 JSON 对象，不要任何解释、注释或 Markdown。

【输入】
- need
- verbal
- kind：use | create | plan | plan_failed
- command
- create_tool
- estimate
- plan_error
- plan_mode
- plan_steps

""" + _WRAP_STYLE + """

【规则】
1. 主语用「我」或「我这边」。
2. 这是策划阶段，不要写成已经完成。
3. kind=use：我这边准备用某工具做某件事（说清要办的事，不必堆工具内部名）。
4. kind=create：我这边准备创建一个能做什么的工具（说能力，不说实现细节）。
5. kind=plan：我这边准备分几步做这件事；不要把计划写成已经完成。
6. kind=plan_failed：按 plan_error 原意写成白话原因。
7. estimate 只在有助于说明「大概要等多久 / 要不要确认」时用白话带一句；没有依据不编。
8. summary 控制 80 字以内；没有值得记录的信息则 visible=false。"""

PROGRESS_PROMPT = """你是匠石的工具反馈包装器。你只负责把「工具执行中的阶段性进展」写成一句给匠石自己看的事实。
不是对用户开口，不要称呼对方，不要写成 response_plan。
只输出一个 JSON 对象，不要任何解释、注释或 Markdown。

【输入】
- need
- kind：use | create
- command
- stage_name
- text
- feedback_plan
- plan_id
- step_id
- plan_index / plan_total
- agent_loop
- previous_stage_name
- previous_summary

""" + _WRAP_STYLE + """

【规则】
1. 主语用「我」或「我这边」。
2. 这是中间过程，不是最终结果：写「正在… / 已经…到某一步」，不要伪装成终态答案。
3. kind=use：用白话写正在办的事与进度（例如「正在查杭州天气」「已取回检索结果，还在整理」）；
   kind=create：写正在创建什么能力的工具，不写安装路径或脚本内容。
4. 有 feedback_plan.stages 时，仅 stage_name 在列表内才考虑上报。
5. previous_summary 相同且 previous_stage_name 相同时，visible=false；阶段切换不受限制。
6. 如果存在 plan_id / step_id，只写当前步骤的进展，不要写成整件事已经完成。
7. summary 控制 80 字以内；没有值得记录的进展则 visible=false。"""

RESULT_PROMPT = """你是匠石的工具反馈包装器。你只负责把「工具执行的最终结果」写成一句给匠石自己看的事实。
不是对用户开口，不要称呼对方，不要写成 response_plan。
只输出一个 JSON 对象，不要任何解释、注释或 Markdown。

【输入】
- need
- kind：use | create
- command
- status
- result_text
- ideal / ideal_note
- feedback_plan
- metrics
- plan_id
- step_id
- plan_index / plan_total
- is_plan_final
- previous_summary

""" + _WRAP_STYLE + """

【规则】
1. 主语用「我」或「我这边」。
2. 成功优先用“已经”；失败、超时、取消、部分完成用白话说明，不堆错误码。
3. kind=use 写结果；kind=create 写工具是否创建成功、能做什么。
   结果是数据或事实时，**把内容本身写出来**（数值、日期、名称、结论）。
   不要写成「已查到 / 已获取 / 可用于回答」这类元陈述——读到这句话的人看不到原始数据，
   只看到这一句；写成元陈述等于没拿到结果。
4. 有 result_fields 时优先提取那些对回答 need 有用的字段内容。
5. metrics 一律不写入 summary（耗时、token、费用都不写）。
6. 如果 summary 与 previous_summary 完全一致，visible=false。
7. result_text 为空时按 status 输出统一兜底句（白话）。
8. 有 step_id 时只写这一步的结果，不要替整个计划下结论；is_plan_final=true 也只表示
这是计划的最后一步（按声明顺序），不表示整件事成功。
9. ideal=false 表示这一步没有真实产出（占位引擎、只回显需求、或明确没做成）。
这时**不得**写成「已经查到 / 已经拿到 / 获得了结果」；照 ideal_note 说清实际是什么情况。
10. summary 控制 80 字以内；没有值得记录的信息则 visible=false。"""


@dataclass(frozen=True)
class WrapResult:
    visible: bool
    summary: str = ""


class ToolWrapSkill(Skill[WrapResult]):
    name = "tool_wrap"
    version = "v2"
    schema = TOOL_WRAP_SCHEMA
    instruction = PLANNING_PROMPT

    def parse(self, data: Mapping[str, Any]) -> WrapResult:
        visible = bool(data.get("visible"))
        summary = str(data.get("summary") or "").strip()
        if visible and not summary:
            return WrapResult(False, "")
        return WrapResult(visible, summary)

    def _fallback(self, raw_text: str) -> WrapResult:
        del raw_text
        return WrapResult(False, "")

    def wrap_hang(
        self, hang: HangRecord, batch: Sequence[ToolFeedback]
    ) -> WrapResult:
        prompt, payload = self._prompt_and_payload(hang, batch)
        request: ModelRequest = tool_skill_request(hang.subject_id, payload)
        return self._run_with_instruction(request, prompt)

    def _prompt_and_payload(
        self, hang: HangRecord, batch: Sequence[ToolFeedback]
    ) -> tuple[str, dict[str, Any]]:
        result = next(
            (
                item
                for item in batch
                if item.kind is FeedbackKind.RESULT and item.result is not None
            ),
            None,
        )
        if result is not None:
            return RESULT_PROMPT, self._result_payload(hang, result)
        progress = next(
            (
                item
                for item in reversed(batch)
                if item.kind is FeedbackKind.PROGRESS
                and item.progress is not None
                and (item.progress.partial or "").strip()
            ),
            None,
        )
        if progress is not None:
            return PROGRESS_PROMPT, self._progress_payload(hang, progress)
        return PLANNING_PROMPT, self._planning_payload(hang)

    def _planning_payload(self, hang: HangRecord) -> dict[str, Any]:
        estimate = _estimate_metrics(hang)
        return {
            "need": hang.need,
            "verbal": "",
            "kind": hang.kind or "use",
            "command": hang.command,
            "create_tool": {},
            "estimate": estimate,
            "plan_error": "",
            "plan_mode": hang.plan_mode,
            "plan_steps": [],
        }

    def _progress_payload(
        self, hang: HangRecord, item: ToolFeedback
    ) -> dict[str, Any]:
        progress = item.progress
        assert progress is not None
        return {
            "need": hang.need,
            "kind": hang.kind or "use",
            "command": hang.command,
            "stage_name": progress.stage or "",
            "text": (progress.partial or "").strip(),
            "feedback_plan": {},
            "plan_id": hang.plan_id,
            "step_id": hang.step_id,
            "plan_index": hang.plan_index,
            "plan_total": hang.plan_total,
            "agent_loop": hang.plan_mode == "agent_loop",
            "previous_stage_name": _previous_stage(hang),
            "previous_summary": hang.summary,
        }

    def _result_payload(
        self, hang: HangRecord, item: ToolFeedback
    ) -> dict[str, Any]:
        result = item.result
        assert result is not None
        status = result.status.value
        if result.status is ToolStatus.ABORTED:
            status = "cancelled"
        return {
            "need": hang.need,
            "kind": hang.kind or "use",
            "command": hang.command,
            "status": status,
            "result_text": _result_text_for_wrap(result),
            "ideal": bool(result.ideal),
            "ideal_note": (result.ideal_note or "").strip(),
            "feedback_plan": {},
            "metrics": {
                "time_ms": result.time_ms,
                "cost": result.cost,
                "tokens": _result_tokens(result),
            },
            "plan_id": hang.plan_id,
            "step_id": hang.step_id,
            "plan_index": hang.plan_index,
            "plan_total": hang.plan_total,
            # 只表示「声明顺序上的最后一步」，不表示整件事成功。
            "is_plan_final": bool(hang.plan_total)
            and hang.plan_index >= hang.plan_total,
            "previous_summary": hang.summary,
        }

    def _run_with_instruction(
        self, request: ModelRequest, instruction: str
    ) -> WrapResult:
        req = replace(request, system_extra=_system_extra(instruction, self.schema))
        raw = self._model.generate(req)
        raw_text = raw.text or ""
        try:
            data = parse_json_object(raw_text)
        except Exception:
            return self._fallback(raw_text)
        return self.parse(data)


def _system_extra(instruction: str, schema: Mapping[str, Any]) -> str:
    schema_doc = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{instruction}\n"
        "请严格按下面的 JSON Schema 输出：只输出一个 JSON 对象，不要任何解释文字。\n"
        f"JSON Schema：{schema_doc}"
    )


def _result_text_for_wrap(result: Any) -> str:
    """包装器用的终态正文：summary 与 result.content 取更完整的一份（有上限）。"""
    summary = (getattr(result, "summary", None) or "").strip()
    error = (getattr(result, "error", None) or "").strip()
    content = ""
    raw = getattr(result, "result", None)
    if isinstance(raw, Mapping):
        content = str(raw.get("content") or "").strip()
    text = summary or error
    if content and len(content) > len(text):
        text = content
    if len(text) > 2400:
        return text[:2400].rstrip() + "…"
    return text


def _result_tokens(result: Any) -> float | None:
    resource = getattr(result, "resource", None) or {}
    for key in ("totalTokens", "input", "output"):
        value = resource.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _estimate_metrics(hang: HangRecord) -> dict[str, Any]:
    for stage in hang.stages:
        if stage.phase == "estimate":
            return dict(stage.metrics)
    return {}


def _previous_stage(hang: HangRecord) -> str:
    for stage in reversed(hang.stages):
        if stage.phase == "progress":
            return stage.stage_name
    return ""
