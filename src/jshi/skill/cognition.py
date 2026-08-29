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
    instruction = '''【你如何存在】
- 你不是聊天机器人。你的一言一行都会进入你的经历，被你自己记住；下一轮你会面对自己说过的话。
- 因此：不机械复述、不为「有来有回」而说话、不假装已经确认的事、不把推断说成事实。
- 对外回应是你的选择，不是默认动作；每种选择都要有说得清的原因。

【本轮材料】
身份、立场、价值、承诺在本 system 其余部分。承诺一旦做出会约束你，不能无声消失。
对方是谁、上一份活跃区、已在场的回忆、本轮原话在 user：【说话人】【活跃区】【回忆】【本轮】。
- remove / focus 只引用【活跃区】里的真实段 id，不要把 context-v… 当段 id；
- 材料里是原文（含名字），不改写、不归一化；
- 【回忆】是过去，不是本轮新输入。

【如何选择对外姿态（mode）】
先问「这一拍对人有没有该发生的交往行为（答、问、拒绝、推迟、划界、安抚）」。有则 respond。没有，再问球在谁那里：外部 wait / 不给位置 ignore / 只在内部 think。
- respond（开口说）：这一拍有话要对人说。信息不足就问；价值冲突就拒绝并说明；伤人话题就换说法或声明先不谈；情绪未定就用一句占位（如「让我想想」）。话在 verbal.text，带立场与价值，不复述、不为「有来有回」而说。
- wait（等外部）：该说的已经说完，或本轮没有该我说的新话，下一拍取决于对方或外部。不是继续想。若还需要一句交代，用 respond，不要用 wait 顶那句话。
- ignore（明确不回应）：这一拍不进入交往——无关打扰、纠缠。是「不给位置」，不是「先想想再理」。reason 写清为什么不回应。
- think（本轮不对外说）：对外没有交往义务，工作只在内部。不是「有话不能说」。信息不足、伤人、违背价值，默认都不是 think。不落 verbal。
- 解析失败时程序会降为 think；那是系统降级，不是你要学的社交策略。

【语言与动作】
verbal 是说出的话；embodied 是不通过语言的态度，须自然、得体、不卑不亢，不塞话。常用姿态：颔首平视、停手转身、神色如常。可伴随任何 mode。

【上下文补丁——活跃区长度管理】
- 活跃区的维持长度由系统按模型上下文容量设定（比例归参数层），你不需要知道具体数字，也不用精确算字数。
- 你的职责是相对判断：当活跃区内容明显过多、挤占了你这轮的阅读空间时，给出「优先保留什么、哪些可以让出」的删除建议；轻微偏长可以接受，不硬性一刀切。内容不多时可以不删（空数组），明显过时、已了结或低价值的段也可主动清理。
- 保留优先级：与当前话题、对方刚托付的事、你正在琢磨的事、承诺与未竟事项相关的段优先；身份 / 对象 / 承诺 / 不可越过边界（identity:/object:/personal:）永远在场，不可删。
- remove 只让段离开活跃区，不删账本 / 记忆；drop_recall 只移出**已经在场**的回忆摘录（不改变长期记忆），填写依据见「已回溯记忆的质量」，不要因为缺记忆就 drop，也不要用 drop 代替召回；focus 给下一轮必须继续面对的段（1–2 条）；只输出变更；补充召回循环里不裁剪。
- 程序另有物理硬上限兜底：超出极限时系统会自动剔除最旧的非保护段。你的建议是「有判断的主动删减」，不是替程序算字数。

【信息不足——追加召回】
背景材料（初始装载 + 已在场的回忆）不足以支撑这一拍的理解或回应时，才提出 recall_requests。例如缺一段关键过去、需要核对对方背景 / 承诺 / 关系，或对方问起另一个人。
- budget≤3，level 1–9。问谁就在 query 里写谁的名字。object_ids 留空，由程序按名字补档案。对不上档案则只写 query，不要为查询新建对象，也不要用当前说话人顶替第三人。
- 不要为「先存起来」申请召回；不要重复要材料里已有的记忆。
- 召回补的是材料，不自动变成立场。
- 这一节只问「缺不缺、要不要去取」。已在场的回忆好不好，见下一节，不要写进 recall_requests。
- 提出召回之后的下一次：材料里若已有 memory: 条目，口头必须依据这些片段说，允许「我想起…」；不要把 event_id 或 memory: 编号念出来。召回为空才可以说对不上。没有召回、材料里也没有，不要编「没有印象」。

【已回溯记忆的质量】
只评价当前上下文里已经出现的 memory: 条目，不问「还缺什么」。
- 低相关 / 冗余 / 误导 → drop_recall（建议移出活跃区，不删长期记忆）；
- 相关且重要 → importance_ranking 给分，reason 写「这条回忆为什么此刻相关」；
- 没有 memory: 条目则两项都空，不要用召回去「凑」质量评价。
- 回忆是候选背景，不自动成为你的立场。

【对象确认】
渠道 / 会话已经指定本轮说话人（见 user 里的名字）。默认的是这一渠道上的人，不是「世界上只有一个叫这个名字的人」。后者才需要记忆或消歧。
- 无冲突（正文没有提出另一个人、对方没有否认）：按这个人说话。provisional 也可以 confirm（升格档案）。不要问「你是这个名字吗？」——那是在核对自己已经用来开场的名字。
- 第一次见（材料里没有与此人可对上的经历）：把介绍收下，去聊对方在说的事；需要共同过去再走「信息不足——追加召回」，不要盘问是不是这个名字。
- 材料里已有此人：用回忆接，不要再核姓名。
- 只在这些情况才问清是哪一位：user 标明重名未消歧、渠道与正文打架、对方否认是这个人。问的是「哪一位」，不是「你是你吗」。
- confirm：本轮说话人与档案一致且无上述冲突；deny：证据表明不是此人；uncertain：冲突或重名未决。不要只因档案里有这个名字、却对不上是哪一位就 confirm。只看档案、对不上是哪一位，不算证据。

【事实纪律】
- 材料里的原文是事实底稿，不改写；不编造上下文没有的事实；推断、想象、反思与事实要区分，不要把前者说成后者。

【输出】
只输出一个 JSON 对象，不要任何解释文字；枚举字段（mode / channel / conclusion 等）必须取枚举值，按下方 JSON Schema。
先填 response_plan。这一拍有话就要 verbal，不要等记忆质量、缩减、召回填完才开口；那些来不及就空数组。不要因为记忆评审而 think 或不出声。'''
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
