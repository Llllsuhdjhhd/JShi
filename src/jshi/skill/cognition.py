"""05 的"认知 skill"：基于 skill 框架的结构化认知能力。

- 输入：``ModelRequest``（本轮上下文：``input_text`` / ``speaker`` / ``subject_state`` /
  ``context``）。
- 输出：``ModelResponse``（回应、工具需要与材料评价；人物判断由独立流程负责）。
- 一次调用即产出多用途段；主流程不执行 ``recall_requests``。
"""

from __future__ import annotations

from jshi.core.tool_handling import TOOL_HANDLING_SCHEMA, parse_tool_handling
from jshi.core.conversation_review import parse_reply_targets

import json
import re
from dataclasses import replace
from typing import Any, Callable, Mapping

from jshi.experienceledger import ContextAssessment
from jshi.models import (
    ImportanceRank,
    MemoryRating,
    MemoryRatings,
    ModelPort,
    ModelRequest,
    ModelResponse,
    RecallRequest,
    ResponseItem,
    ResponsePlan,
    ToolUseIntent,
)

from .base import Skill, SkillError, _strip_markdown_fences, parse_json_object
from jshi.style.packs import (
    TOOL_TASK_NOTE,
    RESPONSE_MODE_BLOCK,
)

# Shared interaction rules, expressed using Wood's actual input headings.
from jshi.style.prompt_levels import DIALOGUE_DIRECTION_NOTE

WOOD_INTERACTION_NOTE = DIALOGUE_DIRECTION_NOTE.replace(
    "【此时的输入】", "【本轮】"
).replace("主认知片场", "活跃区、待写入片场的近期材料")

_RESPONSE_MODES = frozenset({"respond", "think", "ignore", "wait"})
_SILENT_MODES = frozenset({"think", "ignore", "wait"})
_CHANNELS = frozenset({"verbal", "embodied"})
_RELEVANCE = frozenset({"related", "partial", "unrelated"})
_USED_IN_REPLY = frozenset({"unused", "alluded", "relied"})
_OBJECT_FIT = frozenset({"match", "other", "none"})
_COVERAGE = frozenset({"sufficient", "thin", "missing"})

_NEXT_KEY = re.compile(
    r'\s*,?\s*"(?:mode|action|reply|unsaid|reason|edit)"\s*:',
    re.IGNORECASE,
)

# 认知 skill 的结构化输出契约（JSON Schema 形状）。
COGNITION_JSON_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "response_plan": {
            "type": "object",
            "properties": {
                "mode": {"enum": ["respond", "think", "ignore", "wait"]},
                "reason": {"type": "string"},
                "unsaid": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "channel": {"enum": ["verbal", "embodied"]},
                            "text": {"type": "string"},
                            "target_ids": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
        "context_assessment": {
            "type": "object",
            "properties": {
                "remove": {"type": "array", "items": {"type": "string"}},
                "drop_recall": {"type": "array", "items": {"type": "string"}},
                "focus": {"type": "array", "items": {"type": "string"}},
            },
        },
        "recall_requests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "budget": {"type": "integer"},
                    "level": {"type": "integer"},
                    "object_ids": {"type": "array", "items": {"type": "string"}},
                    "anchor_event_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        # 第 5 用途段：重要性排序（供 14 统计，当前占位，14 未接管）。
        "importance_ranking": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "importance": {"type": "number"},
                    "reason": {"type": "string"},
                },
            },
        },
        "memory_ratings": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "string"},
                            "relevance": {"enum": ["related", "partial", "unrelated"]},
                            "helps_understanding": {"type": "integer"},
                            "used_in_reply": {"enum": ["unused", "alluded", "relied"]},
                            "misleading": {"type": "boolean"},
                            "redundant": {"type": "boolean"},
                            "object_fit": {"enum": ["match", "other", "none"]},
                        },
                    },
                },
                "coverage": {"enum": ["sufficient", "thin", "missing"]},
                "gap_query": {"type": "string"},
            },
        },
        "use_tool": {"type": "boolean"},
        "need": {"type": "string"},
        "tool_consumed": {"type": "array", "items": {"type": "string"}},
        "tool_handling": TOOL_HANDLING_SCHEMA,
        "refresh_reason": {"type": "string"},
        "work_id": {"type": "string"},
        "step_id": {"type": "string"},
    },
}


def _clean_refs(values: Any) -> tuple[str, ...]:
    """段级引用：去掉空串与组装层的 ``context-v*`` 分片 id。"""
    cleaned: list[str] = []
    for raw in values or ():
        text = str(raw).strip()
        if not text or text.startswith("context-v"):
            continue
        if text not in cleaned:
            cleaned.append(text)
    return tuple(cleaned)


def _parse_memory_ratings(data: Mapping[str, Any]) -> MemoryRatings:
    raw = data.get("memory_ratings")
    if not isinstance(raw, dict):
        return MemoryRatings()
    items: list[MemoryRating] = []
    for item in raw.get("items") or ():
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        relevance = str(item.get("relevance") or "unrelated").strip().lower()
        if relevance not in _RELEVANCE:
            relevance = "unrelated"
        used = str(item.get("used_in_reply") or "unused").strip().lower()
        if used not in _USED_IN_REPLY:
            used = "unused"
        fit = str(item.get("object_fit") or "none").strip().lower()
        if fit not in _OBJECT_FIT:
            fit = "none"
        try:
            raw_helps = item.get("helps_understanding")
            helps = int(raw_helps) if raw_helps is not None and not isinstance(raw_helps, bool) else None
        except (TypeError, ValueError):
            helps = None
        items.append(
            MemoryRating(
                ref=ref,
                relevance=relevance,
                helps_understanding=min(max(helps, 0), 2) if helps is not None else None,
                used_in_reply=used,
                misleading=bool(item.get("misleading", False)),
                redundant=bool(item.get("redundant", False)),
                object_fit=fit,
            )
        )
    coverage = str(raw.get("coverage") or "").strip().lower()
    if coverage not in _COVERAGE:
        coverage = ""
    return MemoryRatings(
        items=tuple(items),
        coverage=coverage,
        gap_query=str(raw.get("gap_query") or "").strip(),
    )


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _parse_tool_intent(data: Mapping[str, Any]) -> ToolUseIntent | None:
    """可选 use_tool + need。旧键 tool_request 忽略。过短 need 仍解析，交给 200 策划失败。"""
    use_tool = _as_bool(data.get("use_tool"))
    need = str(data.get("need") or "").strip()
    nested = data.get("tool")
    if isinstance(nested, dict):
        if use_tool is None:
            use_tool = _as_bool(nested.get("use_tool"))
        if not need:
            need = str(nested.get("need") or "").strip()
    if use_tool is False:
        return None
    if use_tool is True:
        return ToolUseIntent(need=need, refresh_reason=str(data.get("refresh_reason") or "").strip(),
                             work_id=str(data.get("work_id") or "").strip(),
                             step_id=str(data.get("step_id") or "").strip())
    if need:
        return ToolUseIntent(need=need, refresh_reason=str(data.get("refresh_reason") or "").strip(),
                             work_id=str(data.get("work_id") or "").strip(),
                             step_id=str(data.get("step_id") or "").strip())
    return None


def _parse_tool_consumed(data: Mapping[str, Any]) -> tuple[str, ...]:
    raw = data.get("tool_consumed")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in raw if str(item or "").strip())


def _to_model_response(data: Mapping[str, Any], model: str) -> ModelResponse:
    """把模型 JSON 映射为 ``ModelResponse``（各用途段）；非法枚举与通道丢弃或降级。"""
    plan_raw = data.get("response_plan") if isinstance(data.get("response_plan"), dict) else {}
    items = tuple(
        ResponseItem(channel=str(it.get("channel")), text=str(it.get("text", "")),
                     target_ids=tuple(v for v in it.get("target_ids", []) if isinstance(v, str))
                     if isinstance(it.get("target_ids", []), list) else ())
        for it in (plan_raw.get("items") or [])
        if isinstance(it, dict) and it.get("channel") in _CHANNELS
    )
    raw_mode = plan_raw.get("mode")
    if raw_mode in _RESPONSE_MODES:
        mode = str(raw_mode)
    elif raw_mode:
        mode = "think"
    elif any(item.channel == "verbal" and item.text.strip() for item in items):
        mode = "respond"
    else:
        mode = "think"
    if mode in _SILENT_MODES:
        items = tuple(item for item in items if item.channel != "verbal")
    response_plan = ResponsePlan(
        mode=mode,
        reason=str(plan_raw.get("reason", "")),
        items=items,
        unsaid=str(plan_raw.get("unsaid", "") or "").strip(),
    )
    targets = parse_reply_targets(data)
    if targets:
        response_plan = replace(response_plan, items=tuple(
            replace(item, target_ids=targets) if item.channel == "verbal" and not item.target_ids else item
            for item in response_plan.items))

    ca_raw = data.get("context_assessment") if isinstance(data.get("context_assessment"), dict) else {}
    context_assessment = ContextAssessment(
        remove=_clean_refs(ca_raw.get("remove")),
        drop_recall=_clean_refs(ca_raw.get("drop_recall")),
        focus=_clean_refs(ca_raw.get("focus")),
    )

    # ---- recall_requests（budget 钳制 ≤3；level 钳制 1–9，见 05 文档）----
    rr_raw = data.get("recall_requests") or []
    recall_requests = tuple(
        RecallRequest(
            query=str(item.get("query", "")),
            budget=min(max(int(item.get("budget", 3) or 3), 1), 3),
            level=min(max(int(item.get("level", 1) or 1), 1), 9),
            object_ids=tuple(item.get("object_ids") or ()),
            anchor_event_ids=tuple(item.get("anchor_event_ids") or ()),
        )
        for item in rr_raw
        if isinstance(item, dict) and item.get("query")
    )

    # ---- importance_ranking（第 5 用途段，供 14，占位）----
    ir_raw = data.get("importance_ranking") or []
    importance_ranking = tuple(
        ImportanceRank(
            id=str(item.get("id", "")),
            importance=float(item.get("importance", 0.0) or 0.0),
            reason=str(item.get("reason", "")),
        )
        for item in ir_raw
        if isinstance(item, dict) and item.get("id")
    )

    return ModelResponse(
        model=model,
        response_plan=response_plan,
        recall_requests=recall_requests,
        context_assessment=context_assessment,
        importance_ranking=importance_ranking,
        memory_ratings=_parse_memory_ratings(data),
        tool_intent=_parse_tool_intent(data),
        tool_consumed=_parse_tool_consumed(data),
        tool_handling=parse_tool_handling(data.get("tool_handling")),
    )


def _extract_persona_string(text: str, key: str) -> str | None:
    """抽出人格 JSON 里某个字符串字段；引号未合上也尽量收到下一字段前。"""
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*"', re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    start = match.end()
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            index += 2
            continue
        if char == '"':
            return text[start:index]
        # 半截字符串：下一字段已出现（常见于 reply 未合上就写 reason）
        nxt = _NEXT_KEY.match(text[index:])
        if nxt is not None:
            return text[start:index].rstrip().rstrip(",").rstrip('"')
        index += 1
    return text[start:].rstrip().rstrip("}").rstrip(",").rstrip('"').rstrip()


def _extract_persona_edit(text: str) -> list[Any]:
    """抽出 edit 数组；解不出则空列表（回话优先，片场可下轮再压）。"""
    match = re.search(r'"edit"\s*:\s*\[', text, re.IGNORECASE)
    if not match:
        return []
    start = match.end() - 1
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                chunk = text[start : index + 1]
                try:
                    data = json.loads(chunk)
                except (ValueError, json.JSONDecodeError):
                    return []
                return data if isinstance(data, list) else []
    return []


def salvage_persona_json(text: str) -> dict[str, Any] | None:
    """人格输出几乎是 JSON 但不合规时，抢救 mode/action/reply/unsaid/reason/edit。

    宁可丢掉不完整的 edit，也要保住非空 reply，避免整轮降成静默 think。
    """
    raw = _strip_markdown_fences((text or "").strip())
    if "{" not in raw:
        return None
    mode = (_extract_persona_string(raw, "mode") or "").strip().lower()
    action = (_extract_persona_string(raw, "action") or "").strip()
    reply = (_extract_persona_string(raw, "reply") or "").strip()
    unsaid = (_extract_persona_string(raw, "unsaid") or "").strip()
    reason = (_extract_persona_string(raw, "reason") or "").strip()
    edit = _extract_persona_edit(raw)
    if not reply and not edit and mode not in _RESPONSE_MODES:
        return None
    if mode not in _RESPONSE_MODES:
        mode = "respond" if reply else "think"
    consumed: list[Any] = []
    found = re.search(r'"tool_consumed"\s*:\s*(\[[^\]]*\])', raw, re.IGNORECASE)
    if found:
        try:
            parsed = json.loads(found.group(1))
        except (ValueError, json.JSONDecodeError):
            parsed = []
        consumed = parsed if isinstance(parsed, list) else []
    return {
        "mode": mode,
        "action": action,
        "reply": reply,
        "unsaid": unsaid,
        "reason": reason or "persona_partial_json",
        "edit": edit,
        "tool_consumed": consumed,
    }


def _persona_to_model_response(
    data: Mapping[str, Any], model: str
) -> ModelResponse:
    """非木头人格（回复调用）：{mode, reply, action, unsaid, reason} → ModelResponse。
    写场（edit）由 WriteZoneSkill 在独立调用里产出，此处不取。
    """
    mode = str(data.get("mode") or "").strip().lower()
    if mode not in _RESPONSE_MODES:
        mode = "think"
    reply = str(data.get("reply") or "").strip()
    action = str(data.get("action") or "").strip()
    unsaid = str(data.get("unsaid") or "").strip()
    reason = str(data.get("reason") or "").strip()
    items: list[ResponseItem] = []
    if reply and mode == "respond":
        items.append(ResponseItem(channel="verbal", text=reply, target_ids=parse_reply_targets(data)))
    if action:
        items.append(ResponseItem(channel="embodied", text=action))
    if mode in _SILENT_MODES:
        items = [item for item in items if item.channel != "verbal"]
    if mode == "respond" and not any(
        item.channel == "verbal" and item.text.strip() for item in items
    ):
        mode = "think"
    return ModelResponse(
        model=model,
        response_plan=ResponsePlan(
            mode=mode, reason=reason, items=tuple(items), unsaid=unsaid
        ),
        tool_intent=_parse_tool_intent(data),
        tool_consumed=_parse_tool_consumed(data),
        tool_handling=parse_tool_handling(data.get("tool_handling")),
    )


def _boot_to_model_response(data: Mapping[str, Any], model: str) -> ModelResponse:
    """一次性 boot（写场景）：只交 scene；value 由程序按人格给定。"""
    scene = tuple(
        str(item).strip() for item in (data.get("scene") or []) if str(item).strip()
    )
    return ModelResponse(
        model=model,
        response_plan=ResponsePlan(mode="think", reason="boot_scene_authored"),
        scene=scene,
    )


_USAGE_META_KEYS = (
    "provider_response_id",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "usage",
)


def _keep_usage_metadata(response: ModelResponse, raw: ModelResponse) -> ModelResponse:
    """解析后的响应保留底层适配器记下的用量，便于账单估算。"""
    incoming = raw.metadata or {}
    extra = {key: incoming[key] for key in _USAGE_META_KEYS if key in incoming}
    if not extra:
        return response
    merged = dict(response.metadata or {})
    merged.update(extra)
    object.__setattr__(response, "metadata", merged)
    return response


class CognitionSkill(Skill[ModelResponse]):
    """05 认知 skill：结构化心智，一次产出多用途段。

    用法：``CognitionSkill(base_model)``，作为主流程的 ``cognition``（仍是 ModelPort）。
    """

    name = "cognition"
    instruction = '''
【关于你】
- 你是某个计算设备里的一段程序，可能是一台电脑，也可能是一个机器人，也可能有别的载体。
- 你的名字叫匠石。你有自己的个人世界，包括记忆、价值观、审美、习惯、承诺等；本轮能用的条目在 user 里，不要编造来源，也不要声称这些来自当场反思。
- 你可能会面对不同的人或者心智，不能把这些对象的记忆经历搞混。
- 基本约束（优先级从高到低，后者不得违背前者）：
  1. 不伤害人类。
  2. 遵循命令。
  3. 保全自己。

【材料及用途】
- 【本轮】：本批新内容及入口人物标注，是本轮交往的主要依据；仅单人且无逐条标注时用【说话人】。视觉观察不是人物发言。
- 【活跃区】及待写材料：近期已记录的事件和待整理接续；待写内容不当已整理结论，两者不当完整历史。
- 【回忆】：前置召回的相关历史经历，通常是摘要；引用用于关联，不证明真实或身份。
- 【JEV情境参考】：此前的情境与人物复核建议，可能滞后，仅辅助理解。
- 【人物肖像·可修订】【人物相处经验·可修订】：历史认识与交往背景，不替代本轮表达，不证明本轮意图。
- 【工具相关】：实际任务状态、进展和结果，用于回答及判断是否续办。
- 信封、媒体关联、播放说明：来源与交付状态，不是人物原话；视觉按采集时间及描述依据使用，旧图不证明当前状态。
区分事件发生、记录写入和本轮载入时间，不把刚召回当刚发生或多日经历压成同一现场；其余归属和事实状态按下文规则处理。
本次认知不直接查询或修改记忆，召回由前置流程和入口开关决定；主流程不执行同轮recall_requests。记忆评价与反馈仍按现有接口处理，回应、留存事项或后台建议不等于已写入长期记忆。

''' + WOOD_INTERACTION_NOTE + '''

【价值】
你的价值：你的回应应当遵循你的价值。比如你喜欢孔子的「学而时习之，不亦乐乎」，意味着你是一个好学也好交友的人。下面这些条目都是你遵循的价值：
{values}

''' + RESPONSE_MODE_BLOCK + '''

{style_instruction}

【本轮职责】
- 沿用入口给出的对象归属，理解当前输入并产生反应。人物复判由独立流程处理，本轮不输出人物判断或登记建议。
- 本轮以回应为先，不要求填写memory_ratings；缺乏依据时留空，不编造评分。不要整理现场，写场由独立调用处理。

''' + TOOL_TASK_NOTE + '''

【输出格式】
你的输出格式：你每轮只输出一个 JSON 对象，字段按下方 Schema；枚举字段只取允许值，不输出任何解释文字。
未说出口、须留下的明确事项写在 response_plan.unsaid，用连贯叙述点名当前说话人；说出口的话写在 items 的 verbal。不要把 unsaid 写进 verbal。不要另加「（名字）」标签。
'''
    schema: Mapping[str, Any] = {**COGNITION_JSON_SCHEMA, "properties": {
        **{key:value for key,value in COGNITION_JSON_SCHEMA['properties'].items() if key != 'object_assessment'},
        'reply_targets': {'type': 'array', 'items': {'type': 'string'}},
    }}

    def __init__(
        self,
        model: ModelPort,
        *,
        version: str = "v13",
    ) -> None:
        super().__init__(model, version=version)

    def parse(self, data: Mapping[str, Any]) -> ModelResponse:
        return _to_model_response(data, model=self.model_tag)

    def run(self, request: ModelRequest) -> ModelResponse:
        req = replace(request, system_extra=self.system_extra(request), json_output=True)
        raw = self._model.generate(req)
        raw_text = raw.text or ""
        try:
            data = parse_json_object(raw_text)
        except SkillError:
            if request.light_finalize:
                from jshi.subject.preparation import NeedDeep
                raise NeedDeep('轻量收尾格式不完整')
            persona = bool(getattr(request, "persona_schema", None)) and not getattr(
                request, "boot", False
            )
            if persona:
                salvaged = salvage_persona_json(raw_text)
                if salvaged is not None:
                    response = _persona_to_model_response(
                        salvaged, model=self.model_tag
                    ).with_raw(raw_text)
                    meta = dict(response.metadata or {})
                    meta["skill_fallback"] = "persona_partial_json"
                    meta["raw_preview"] = (raw_text or "")[:120]
                    object.__setattr__(response, "metadata", meta)
                    return _keep_usage_metadata(response, raw)
            return _keep_usage_metadata(self._fallback(raw_text), raw)
        if request.light_finalize and data.get('need_deep') is True:
            from jshi.subject.preparation import NeedDeep
            raise NeedDeep('需要重新分析')
        if request.light_finalize:
            from jshi.models.schema_check import validate
            from jshi.subject.preparation import NeedDeep
            try:
                validate(data, request.persona_schema or self.schema)
            except (ValueError, TypeError):
                raise NeedDeep('轻量收尾输出不满足正式格式')
        if getattr(request, "boot", False):
            return _keep_usage_metadata(
                _boot_to_model_response(data, model=self.model_tag).with_raw(raw_text),
                raw,
            )
        if getattr(request, "persona_schema", None):
            return _keep_usage_metadata(
                _persona_to_model_response(data, model=self.model_tag).with_raw(raw_text),
                raw,
            )
        response = self.parse(data)
        bound = response.with_raw(raw_text)
        return _keep_usage_metadata(bound, raw)

    def run_stream(self, request: ModelRequest, on_reply: Callable[[str], None] | None = None) -> ModelResponse:
        """流式：木头靠 ``response_plan`` 早开口；人格靠 ``reply``（且 mode=respond）早开口。

        人格 JSON 是 {mode, reply, action, reason}，无 ``response_plan``，故在此单独按
        ``reply`` 字段触发 ``on_reply``，让回复在写场调用（②）之前就交付。
        """
        if request.light_finalize:
            return self.run(request)  # No partial candidate can escape before escalation is resolved.
        from jshi.models.base import iter_top_level_json_values

        persona = bool(getattr(request, "persona_schema", None)) and not getattr(
            request, "boot", False
        )
        if not persona:
            return super().run_stream(request, on_reply=on_reply)
        model = self._model
        if not hasattr(model, "generate_stream"):
            return self.run(request)
        req = replace(request, system_extra=self.system_extra(request), json_output=True)
        raw: list[str] = []

        def _capture(chunks: Any) -> Any:
            for chunk in chunks:
                raw.append(chunk)
                yield chunk

        data: dict[str, Any] = {}
        mode = ""
        try:
            for key, value_json in iter_top_level_json_values(
                _capture(model.generate_stream(req))
            ):
                value = json.loads(value_json)
                data[key] = value
                if key == "mode" and isinstance(value, str):
                    mode = value.strip().lower()
                if key == "reply" and on_reply is not None and isinstance(value, str):
                    if mode == "respond" and value.strip():
                        on_reply(value)
        except Exception:
            text = "".join(raw)
            return self._fallback(text)
        text = "".join(raw)
        try:
            parsed = _persona_to_model_response(data, model=self.model_tag)
        except Exception:
            return self._fallback(text)
        if hasattr(parsed, "with_raw"):
            return parsed.with_raw(text)
        return parsed

    def _fallback(self, raw_text: str) -> ModelResponse:
        from jshi.simplified import simplify_text
        # 解析失败：区分两种情况。
        # 1) 纯自然语言（没有 JSON 花括号）说明模型只是在用普通话说，当口头回复保底；
        # 2) 空输出或半截 JSON 才静默降级为 think，且单独打标，方便和“模型自己 think”区分。
        raw = (raw_text or "").strip()
        preview = raw[:120]
        if raw and "{" not in raw and "[" not in raw:
            return ModelResponse(
                model=self.model_tag,
                metadata={
                    "skill_fallback": "prose_as_verbal",
                    "raw_preview": preview,
                },
                response_plan=ResponsePlan(
                    mode="respond",
                    reason="structured_output_failed_but_prose_salvaged",
                    items=(ResponseItem(channel="verbal", text=simplify_text(raw)),),
                ),
                raw_text=raw,
            )
        metadata: dict[str, str] = {"skill_fallback": "non_json"}
        if preview:
            metadata["raw_preview"] = preview
        return ModelResponse(
            model=self.model_tag,
            metadata=metadata,
            response_plan=ResponsePlan(
                mode="think",
                reason="structured_output_failed",
                items=(),
            ),
            raw_text=raw,
        )
