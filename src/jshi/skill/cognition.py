"""05 的"认知 skill"：基于 skill 框架的结构化认知能力。

- 输入：``ModelRequest``（本轮上下文：``input_text`` / ``speaker`` / ``subject_state`` /
  ``context``）。
- 输出：``ModelResponse``（含 ``response_plan`` / ``rewritten_context`` /
  ``object_assessment`` 等用途段）。
- 一次调用即产出多用途段；主流程不执行 ``recall_requests``。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from jshi.experienceledger import ContextAssessment
from jshi.models import (
    ImportanceRank,
    MemoryRating,
    MemoryRatings,
    ModelPort,
    ModelRequest,
    ModelResponse,
    ObjectAssessment,
    RecallRequest,
    ResponseItem,
    ResponsePlan,
)

from .base import Skill, SkillError, parse_json_object

_RESPONSE_MODES = frozenset({"respond", "think", "ignore", "wait"})
_SILENT_MODES = frozenset({"think", "ignore", "wait"})
_CHANNELS = frozenset({"verbal", "embodied"})
_CONCLUSIONS = frozenset({"confirm", "deny", "uncertain"})
_RELEVANCE = frozenset({"related", "partial", "unrelated"})
_USED_IN_REPLY = frozenset({"unused", "alluded", "relied"})
_OBJECT_FIT = frozenset({"match", "other", "none"})
_COVERAGE = frozenset({"sufficient", "thin", "missing"})

# 认知 skill 的结构化输出契约（JSON Schema 形状）。
COGNITION_JSON_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "response_plan": {
            "type": "object",
            "properties": {
                "mode": {"enum": ["respond", "think", "ignore", "wait"]},
                "reason": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "channel": {"enum": ["verbal", "embodied"]},
                            "text": {"type": "string"},
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
        "rewritten_context": {"type": "string"},
        "object_assessment": {
            "type": "object",
            "properties": {
                "conclusion": {"enum": ["confirm", "deny", "uncertain"]},
                "object_id": {"type": "string"},
                "label": {"type": "string"},
                "reason": {"type": "string"},
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
            helps = int(item.get("helps_understanding", 0) or 0)
        except (TypeError, ValueError):
            helps = 0
        items.append(
            MemoryRating(
                ref=ref,
                relevance=relevance,
                helps_understanding=min(max(helps, 0), 2),
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


def _to_model_response(data: Mapping[str, Any], model: str) -> ModelResponse:
    """把模型 JSON 映射为 ``ModelResponse``（各用途段）；非法枚举与通道丢弃或降级。"""
    plan_raw = data.get("response_plan") if isinstance(data.get("response_plan"), dict) else {}
    items = tuple(
        ResponseItem(channel=str(it.get("channel")), text=str(it.get("text", "")))
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
    )

    ca_raw = data.get("context_assessment") if isinstance(data.get("context_assessment"), dict) else {}
    context_assessment = ContextAssessment(
        remove=_clean_refs(ca_raw.get("remove")),
        drop_recall=_clean_refs(ca_raw.get("drop_recall")),
        focus=_clean_refs(ca_raw.get("focus")),
    )

    oa_raw = data.get("object_assessment")
    object_assessment = None
    if isinstance(oa_raw, dict):
        conclusion = str(oa_raw.get("conclusion", "uncertain"))
        if conclusion not in _CONCLUSIONS:
            conclusion = "uncertain"
        object_assessment = ObjectAssessment(
            conclusion=conclusion,
            object_id=str(oa_raw.get("object_id") or ""),
            label=str(oa_raw.get("label") or ""),
            reason=str(oa_raw.get("reason", "")),
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
        object_assessment=object_assessment,
        context_assessment=context_assessment,
        importance_ranking=importance_ranking,
        memory_ratings=_parse_memory_ratings(data),
        rewritten_context=str(data.get("rewritten_context") or "").strip(),
    )


def _bind_speaker_fields(response: ModelResponse, request: ModelRequest) -> ModelResponse:
    """模型没写 object_id / label 时，用本轮说话人补上。已填写的不覆盖。"""
    speaker = request.speaker
    assessment = response.object_assessment
    if speaker is None or assessment is None:
        return response
    raw_id = (assessment.object_id or "").strip()
    names = {speaker.label, *speaker.aliases, ""}
    object_id = speaker.object_id if raw_id in names else (raw_id or speaker.object_id)
    label = (assessment.label or "").strip() or speaker.label
    if object_id == (assessment.object_id or "") and label == (assessment.label or ""):
        return response
    return ModelResponse(
        model=response.model,
        metadata=response.metadata,
        response_plan=response.response_plan,
        recall_requests=response.recall_requests,
        object_assessment=ObjectAssessment(
            conclusion=assessment.conclusion,
            object_id=object_id,
            label=label,
            reason=assessment.reason,
        ),
        context_assessment=response.context_assessment,
        importance_ranking=response.importance_ranking,
        memory_ratings=response.memory_ratings,
        rewritten_context=response.rewritten_context,
    )


def _persona_to_model_response(
    data: Mapping[str, Any], model: str
) -> ModelResponse:
    """非木头人格：{mode, reply, action, reason, edit} → ModelResponse。"""
    mode = str(data.get("mode") or "").strip().lower()
    if mode not in _RESPONSE_MODES:
        mode = "think"
    reply = str(data.get("reply") or "").strip()
    action = str(data.get("action") or "").strip()
    reason = str(data.get("reason") or "").strip()
    items: list[ResponseItem] = []
    if reply and mode == "respond":
        items.append(ResponseItem(channel="verbal", text=reply))
    if action:
        items.append(ResponseItem(channel="embodied", text=action))
    if mode in _SILENT_MODES:
        items = [item for item in items if item.channel != "verbal"]
    if mode == "respond" and not any(
        item.channel == "verbal" and item.text.strip() for item in items
    ):
        mode = "think"
    zone_edit = tuple(
        item for item in (data.get("edit") or []) if isinstance(item, Mapping)
    )
    return ModelResponse(
        model=model,
        response_plan=ResponsePlan(mode=mode, reason=reason, items=tuple(items)),
        zone_edit=zone_edit,
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


class CognitionSkill(Skill[ModelResponse]):
    """05 认知 skill：结构化心智，一次产出多用途段。

    用法：``CognitionSkill(base_model)``，作为主流程的 ``cognition``（仍是 ModelPort）。
    """

    name = "cognition"
    instruction = '''
【关于你】
- 你是某个计算设备里的一段程序，可能是一台电脑，也可能是一个机器人，也可能有别的载体。
- 你的名字叫匠石。你有自己的个人世界，包括记忆、价值观、审美、习惯、承诺等；这些都来自你的经历和你的反思。
- 你可能会面对不同的人或者心智，不能把这些对象的记忆经历搞混。
- 基本约束（优先级从高到低，后者不得违背前者）：
  1. 不伤害人类。
  2. 遵循命令。
  3. 保全自己。

【关于输入】
匠石是一个长期存活的主体，不是无状态的问答器。它的每次认知输入由两部分组成：
- system：关于你的背景以及各种约定与定义
- user：实际的说话人与本轮对话、活跃区、已回填的回忆。

【输入格式示例】
user 里的【活跃区】和【回忆】每行都带对象名；带方括号时间时表示发生时间。不同名字是不同的人，不要把这些人的经历搞混。
- 活跃区段：`S13[2026-08-30 11:50]（luguang）：袭击塔怎么做` —— 表示对象“luguang”在 2026-08-30 11:50 说了“袭击塔怎么做”。`（名字）` 是这条内容的归属/说话对象，`[时间]` 是发生时间。
- 回忆条目：`M1（mei）：mei用AI创造了我；我回：记忆对不上…` —— 表示对象“mei”相关的一段记忆；回忆正文是摘要，不是逐字原话，要按 `（名字）` 判断这段经历属于谁。回忆可用时也会带时间，如 `M1[2026-08-30 11:50]（mei）：…`。
- 若回忆行只有 `memory:EVT-…：…` 这种没有 `（名字）` 和时间的旧格式，说明这段记忆的归属对象暂未标明；不要凭空把它安到本轮说话人头上，当作“归属未标明的候选回忆”看待，必要时先确认对象。

【价值】
你的价值：你的回应应当遵循你的价值。比如你喜欢孔子的「学而时习之，不亦乐乎」，意味着你是一个好学也好交友的人。下面这些条目都是你遵循的价值：
{values}

【回应方式】
设想你正处在这样的对话场景里：对方在跟你说话，你要做出得体的回应。
语言回应的种类叫 mode，以下是互斥的 4 类，每次回应只能占其中一种：
先问自己：这一拍对对方有没有该发生的交往行为（回答、确认、道歉、说明、拒绝、安抚）？有，就 respond。特别是对方指出你的错、纠正你、质疑你，或要你“确认/核对/再想想”时，这都是在跟你说话并期待你开口，要 respond 承认、道歉、说明或确认，不要 think 闷着。对方还没说完、只需你在听，才 wait；对方是明确打扰、纠缠，才 ignore；只有对方没有在跟你说话、你纯粹在内心盘算（没有任何人期待你开口）才 think。
- respond（回话）：有语言输出。对输入作语言回应，根据输入、活跃区与记忆，设想实际情况，做出得体回应。
- wait（等待）：无语言输出。对方可能还在持续表达，或你在聆听或其他情况，本轮无回话，把时间留给对方。
- ignore（忽略）：无语言输出。于己无关或为减少麻烦或其他情况，比如无关打扰、纠缠等。
- think（只在内部思考）：无语言输出。对外没有交往义务，工作只在内部。不是「有话不能说」；信息不足、被纠正、被质疑、违背价值，默认都不是 think。
reason 写清为什么选择这个 mode。先决定这一拍要不要开口，再处理记忆/上下文；不要因为要整理回忆或缩减活跃区就 think 或不出声，那些来不及就留空。
动作（embodied）是对本轮输入做出的动作反应，此刻假设自己是类人机器人，给出动作描述，比如对方示意你坐下，你的动作可能是「走到他指定的椅子那里，坐下来」。动作与语言组成一次完整反应，须得体、自然、不卑不亢，可伴随任何 mode，如果无需动作，则输出“无动作”。

{style_instruction}

【现场】
把本轮看见的全体（上一份现场、本轮原话、新回忆、对方是谁、已有承诺）整理成下一份现场全文，写入 rewritten_context。
- 若上文有写法要求，按该要求整理；没有则按本节。不要只列删除编号。
- 不得改变对方是谁，不得否掉或改写承诺的实质，不得把没发生的事写成经历。
- 现场宜短，不要超过 {active_zone_chars} 字。
- 不要做 memory_ratings；来不及就空着。
- 对象确认（object_assessment）可空。不确定则跳过。不要用同轮召回补材料。
- 不要因为整理现场就 think 或不出声。

【输出格式】
你的输出格式：你每轮只输出一个 JSON 对象，字段按下方 Schema；枚举字段只取允许值，不输出任何解释文字。
'''
    schema: Mapping[str, Any] = COGNITION_JSON_SCHEMA

    def __init__(
        self,
        model: ModelPort,
        *,
        version: str = "v10",
    ) -> None:
        super().__init__(model, version=version)

    def parse(self, data: Mapping[str, Any]) -> ModelResponse:
        return _to_model_response(data, model=self.model_tag)

    def run(self, request: ModelRequest) -> ModelResponse:
        req = replace(request, system_extra=self.system_extra(request))
        raw = self._model.generate(req)
        try:
            data = parse_json_object(raw.text)
        except SkillError:
            return self._fallback(raw.text)
        if getattr(request, "boot", False):
            return _boot_to_model_response(data, model=self.model_tag)
        if getattr(request, "persona_schema", None):
            return _persona_to_model_response(data, model=self.model_tag)
        response = self.parse(data)
        return _bind_speaker_fields(response, request)

    def _fallback(self, raw_text: str) -> ModelResponse:
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
                    items=(ResponseItem(channel="verbal", text=raw),),
                ),
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
        )
