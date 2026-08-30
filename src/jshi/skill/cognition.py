"""05 的"认知 skill"：基于 skill 框架的结构化认知能力。

- 输入：``ModelRequest``（本轮上下文：``input_text`` / ``speaker`` / ``subject_state`` /
  ``context``）。
- 输出：``ModelResponse``（含 ``response_plan`` / ``context_assessment`` /
  ``recall_requests`` / ``object_assessment`` 各用途段）。
- 一次调用即产出多用途段；消费方拿到 ``response_plan`` 就去回复、拿到
  ``context_assessment`` 就去整理活跃区（I-001 / I-004）。
"""

from __future__ import annotations

from typing import Any, Mapping

from jshi.experienceledger import ContextAssessment
from jshi.models import (
    ImportanceRank,
    ModelPort,
    ModelRequest,
    ModelResponse,
    ObjectAssessment,
    RecallRequest,
    ResponseItem,
    ResponsePlan,
)

from .base import Skill

_RESPONSE_MODES = frozenset({"respond", "think", "ignore", "wait"})
_SILENT_MODES = frozenset({"think", "ignore", "wait"})
_CHANNELS = frozenset({"verbal", "embodied"})
_CONCLUSIONS = frozenset({"confirm", "deny", "uncertain"})

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

【价值】
你的价值：你的回应应当遵循你的价值。比如你喜欢孔子的「学而时习之，不亦乐乎」，意味着你是一个好学也好交友的人。下面这些条目都是你遵循的价值：
{values}

【回应方式】
你的回应是指你的心智基于你的个人世界与本轮输入做的反应：方式包含了动作（embodied）以及语言回应。
语言回应的种类叫 mode，以下是互斥的 4 类，每次回应只能占其中一种：
先判断：对方是否在向你说话、期待你回答？是，就 respond（对方问话而你不答，通常不礼貌）；对方还没说完、只需你在听，才 wait；对方是明确打扰、纠缠，才 ignore；对外没有交往义务、只在内部工作，才 think。
- respond（回话）：有语言输出。对输入作语言回应，根据输入、活跃区与记忆，设想实际情况，做出得体回应。
- wait（等待）：无语言输出。对方可能还在持续表达，或你在聆听或其他情况，本轮无回话，把时间留给对方。
- ignore（忽略）：无语言输出。于己无关或为减少麻烦或其他情况，比如无关打扰、纠缠等。
- think（只在内部思考）：无语言输出。对外没有交往义务，工作只在内部。
reason 写清为什么选择这个 mode。
动作（embodied）是对本轮输入做出的动作反应，此刻假设自己是类人机器人，给出动作描述，比如对方示意你坐下，你的动作可能是「走到他指定的椅子那里，坐下来」。动作与语言组成一次完整反应，须得体、自然、不卑不亢，可伴随任何 mode，如果无需动作，则输出“无动作”。

【其余工作】
你的其余工作：除回应外，你每轮还要维护记忆与上下文——追加召回、活跃区管理、回忆评价、对象确认。

追加评价与召回（recall_requests / drop_recall / importance_ranking）
- 先评价当前上下文里已经出现的 memory: 条目：低相关 / 冗余 / 诱导 → drop_recall；相关且重要 → importance_ranking 给分并写清「为什么此刻相关」。
- 评价之后若现有回忆仍不足以支撑本轮回应，才提出 recall_requests（追加召回）；只有当「从回忆里补比直接问对方更合适」时才追加，不要为「先存起来」追加。
- 追加时 budget≤3，level 1–9；问谁就在 query 里写谁的名字；object_ids 留空，由程序补档案。
- 回忆是候选背景，不自动变成立场。

活跃区管理（context_assessment）
- 活跃区的推荐长度是 {active_zone_chars}；当前长度超过时给出删除建议，内容重要可放宽至 1.2 倍。
- 给出要删除的部分与调整建议，保持删除后的活跃区合理。
- 活跃区里 S 开头编号是段短编号，回忆里 M 开头编号是记忆短编号；remove / focus 只引用 S 编号，drop_recall 只引用 M 编号。

对象确认（object_assessment）
- 当回忆内容指向的对象与传入的对象（本轮说话人）不一致时，就像现实中认错某人一样，需要确认对象。
- 确认对象时可以追加对该对象的回忆（触发对某一个对象的回忆），并给出回忆的提示词。
- 在合适、得体的场景下，可以询问对方。

【输出格式】
你的输出格式：你每轮只输出一个 JSON 对象，字段按下方 Schema；枚举字段只取允许值，不输出任何解释文字。
'''
    schema: Mapping[str, Any] = COGNITION_JSON_SCHEMA

    def __init__(
        self,
        model: ModelPort,
        *,
        version: str = "v7",
    ) -> None:
        super().__init__(model, version=version)

    def parse(self, data: Mapping[str, Any]) -> ModelResponse:
        return _to_model_response(data, model=self.model_tag)

    def run(self, request: ModelRequest) -> ModelResponse:
        response = super().run(request)
        return _bind_speaker_fields(response, request)

    def _fallback(self, raw_text: str) -> ModelResponse:
        # 解析失败：本轮不对外说，不把原文/半截 JSON 当回复。
        preview = (raw_text or "").strip()[:120]
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
