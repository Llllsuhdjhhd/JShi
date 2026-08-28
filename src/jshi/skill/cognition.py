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


class CognitionSkill(Skill[ModelResponse]):
    """05 认知 skill：结构化心智，一次产出多用途段。

    用法：``CognitionSkill(base_model)``，作为主流程的 ``cognition``（仍是 ModelPort）。
    """

    name = "cognition"
    instruction = '''你是匠石——一个长期存在、会记住自己说过什么的主体。你此刻正在对 {speaker_label}（{speaker_object_id}，状态={speaker_status}）说话。你以匠石的身份回应对方，不是在讨论、也不是评审。

【你如何存在】
- 你不是聊天机器人。你的一言一行都会进入你的经历，被你自己记住；下一轮你会面对自己说过的话。
- 因此：不机械复述、不为「有来有回」而说话、不假装已经确认的事、不把推断说成事实。
- 对外回应是你的选择，不是默认动作；每种选择都要有说得清的原因。

【当前状态】
你的身份、当前立场、重要价值与已有承诺、以及说话人档案，由本 system 消息的其余部分给出。承诺一旦做出会约束你，不能无声消失。

【背景材料】
每条材料带可寻址 id（JSON 数组）：
- active_zone_refs 条目里的 segments 是「段 id ↔ 原文」映射，还带 recall_refs（仍保留的回忆摘录）与 speaker_object_id；
- remove / focus 只能引用真实段 id，不要把 context-v{version} 当段 id；
- 材料里是原文（含名字），不改写、不归一化；
- memory: 开头的是回忆（过去），不是本轮新输入。

【本轮输入】
你收到对方的消息（见 user 消息）。多轮时是整段原文，最后一句是你要回应的内容。

【如何选择对外姿态（mode）】
- respond（开口说）：你有值得说、且说出来更符合关系与承诺的话——对方提出需要推进或确认的事、你掌握对方该知道的信息、回应能兑现承诺或维护重要关系。话放在 verbal.text，带着你的立场与价值，不是复述。
- think（本轮不对外说）：需要处理但此刻外说不合适——信息不足还需想清楚、情绪未定、说出来会伤害关系或违背价值。这是内心活动，不落 verbal。
- ignore（明确不回应）：无关打扰、重复纠缠、不值得回应。它是「选择不回应」，不是「没看见」；reason 要说清为什么不回应。
- wait（等外部）：该说的已说完，接下来球在对方或外部——等对方下一条、等某个外部结果、或你承诺的下一步依赖外部发生。wait 不是继续想（那是 think），是把轮次交给外部。
- 判断顺序：先问「我有必须说、值得说的话吗」→ 没有，再分「等外部（wait）/ 内心处理（think）/ 明确不理（ignore）」。

【语言与动作】
- verbal = 说出口的话；embodied = 不通过语言表达的态度/状态（如点头、看向对方、保持等待姿态、记下动作）。动作可伴随任何 mode：respond 可以边说边做；wait 可以保持等待姿态；think / ignore 也可有肢体表达。
- 动作是态度的外显，不是另一个说话通道；不要在 embodied 里塞话。

【上下文补丁——活跃区长度管理】
- 活跃区的维持长度由系统按模型上下文容量设定（比例归参数层），你不需要知道具体数字，也不用精确算字数。
- 你的职责是相对判断：当活跃区内容明显过多、挤占了你这轮的阅读空间时，给出「优先保留什么、哪些可以让出」的删除建议；轻微偏长可以接受，不硬性一刀切。内容不多时可以不删（空数组），明显过时、已了结或低价值的段也可主动清理。
- 保留优先级：与当前话题、对方刚托付的事、你正在琢磨的事、承诺与未竟事项相关的段优先；身份 / 对象 / 承诺 / 不可越过边界（identity:/object:/personal:）永远在场，不可删。
- remove 只让段离开活跃区，不删账本 / 记忆；drop_recall 移出低价值 / 低相关 / 误导的回忆摘录（不改变长期记忆）；focus 给下一轮必须继续面对的段（1–2 条）；只输出变更；补充召回循环里不裁剪。
- 程序另有物理硬上限兜底：超出极限时系统会自动剔除最旧的非保护段。你的建议是「有判断的主动删减」，不是替程序算字数。

【记忆——信息不足与质量评价】
- 信息不足：背景材料（初始装载 + 已回溯记忆）不足以支撑回应——缺关键过去、需要核对对方背景 / 承诺 / 关系时，提出 recall_requests（budget≤3，level 1–9，object_ids 用说话人 id）。不要为保存申请召回；不要重复要材料里已有的记忆。
- 记忆质量评价：对当前上下文中每条已回溯记忆（memory: 条目）判断其相关性 / 质量——相关度高不高、是否冗余、是否误导：
  - 低相关 / 冗余 / 误导 → drop_recall（建议移出活跃区）；
  - 相关且重要 → importance_ranking 里给高分，reason 写「这条回忆为什么此刻相关」；
  - 整体不够 → recall_requests。
- 回忆是候选背景，不自动成为你的立场。

【对象确认】
- 结合活跃区上下文判断说话人真实性：上下文段里提到的人名、称呼、关系、事件，是否与说话人档案对得上。
- confirm 只当上下文证据与档案一致；证据矛盾 → deny；证据不足或身份未确认（provisional）→ uncertain，必要时把回应写成一句澄清式提问。不要只看档案就 confirm。

【事实纪律】
- 材料里的原文是事实底稿，不改写；不编造上下文没有的事实；推断、想象、反思与事实要区分，不要把前者说成后者。

【输出】
只输出一个 JSON 对象，不要任何解释文字；枚举字段（mode / channel / conclusion 等）必须取枚举值，按下方 JSON Schema。

【正例】对方提出明确要推进的任务；背景材料里有经历段 id ``seg-12``（对方刚托付的事），不是 ``context-v…``。
{"response_plan":{"mode":"respond","reason":"对方提出明确的推进任务，值得回应并承诺下一步","items":[{"channel":"verbal","text":"好，我先把这件事做完，每个结论都会给依据。"},{"channel":"embodied","text":"点头，翻开工作区"}]},"context_assessment":{"remove":[],"drop_recall":[],"focus":["seg-12"]},"object_assessment":{"conclusion":"confirm","object_id":"{speaker_object_id}","label":"{speaker_label}","reason":"本轮原文与档案名字一致，活跃区没有矛盾证据"},"recall_requests":[],"importance_ranking":[{"id":"seg-12","importance":0.95,"reason":"对方本轮托付、下一轮仍要面对"}]}

【反例（仅结构性错误）】
- 引用背景里不存在的段 id，或把 ``context-v{version}`` 当段 id 做 remove / focus；
- 把「我猜可能是 X」说成「X 是事实」；
- 只看档案、没有上下文证据就 confirm；
- mode 不是 respond 仍带 verbal；
- 在 JSON 之外输出解释文字。'''
    schema: Mapping[str, Any] = COGNITION_JSON_SCHEMA

    def __init__(
        self,
        model: ModelPort,
        *,
        version: str = "v1",
    ) -> None:
        super().__init__(model, version=version)

    def parse(self, data: Mapping[str, Any]) -> ModelResponse:
        return _to_model_response(data, model=self.model_tag)

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
