"""六问落库：把一次自省写成"内部活动 + 候选"。

落点（设计 §5）：

- 04 ``kind=internal`` 活动 + ``activity_created``；
- ``CognitiveContent``（认识状态仍只到「考虑中」）；
- 历史：``introspection_done``，⑥/⑤b/⑤c 有内容时再写 ``introspection_candidate``；
- 07 收尾 ``introspection_finished``。

**不写** 08 / 100、**不发** ``intake``、**不调** ``append_internal``（会被 30 投进 09，见方案 §5.1）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from .port import (
    IntrospectionAnswer,
    IntrospectionRequest,
    IntrospectionRun,
    IntrospectionScene,
    IntrospectionStatus,
)


def run_introspection(
    *,
    request: IntrospectionRequest,
    scene: IntrospectionScene,
    answer: IntrospectionAnswer,
    repository: object,
    activity_close: object,
    model_tag: str = "",
) -> IntrospectionRun:
    """把现场 + 六问写成记录，返回这次自省的结果。"""
    # 延迟导入：避免 reflection → jshi.subject.__init__ → process → reflection 的环
    from jshi.subject.domain import (
        Activity,
        ActivityKind,
        CognitiveContent,
        CognitiveKind,
        EpistemicStatus,
        EvidenceKind,
        HistoryKind,
        HistoryRecord,
    )

    started = time.perf_counter()
    subject_id = request.subject_id
    activity = Activity(
        subject_id=subject_id,
        kind=ActivityKind.INTERNAL,
        trigger=request.entry_ref,
    )
    repository.add_activity(activity)
    repository.add_history(
        HistoryRecord(
            subject_id=subject_id,
            kind=HistoryKind.SUBJECT,
            event_type="activity_created",
            content={
                "activity_id": activity.id,
                "kind": activity.kind.value,
                "trigger": activity.trigger,
                "status": activity.status.value,
                "introspection": True,
            },
            source_ids=(),
        )
    )

    cognition = CognitiveContent(
        subject_id=subject_id,
        activity_id=activity.id,
        kind=CognitiveKind.EVALUATION,
        content=render_answer(answer),
        epistemic_status=EpistemicStatus.CONSIDERING,
        evidence_kind=EvidenceKind.COGNITIVE_REASONING,
        source_ids=scene.source_ids,
        model=model_tag or None,
    )
    repository.add_cognitive_content(cognition)

    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    repository.add_history(
        HistoryRecord(
            subject_id=subject_id,
            kind=HistoryKind.SUBJECT,
            event_type="introspection_done",
            content={
                "activity_id": activity.id,
                "cognitive_content_id": cognition.id,
                "trigger": request.trigger.value,
                "level": request.level.value,
                "urgency": request.urgency.value,
                "entry_ref": request.entry_ref,
                "entry_at": request.entry_at.isoformat(),
                "segments": len(scene.segments),
                "recalled": len(scene.recalled),
                "answer_mode": answer.mode,
                "degraded": answer.degraded,
                "worth_learning": answer.worth_learning,
                "tool_need": answer.tool_need,
                "interaction_need": _plain(answer.interaction_need),
                "duration_ms": duration_ms,
                "model_tag": model_tag,
            },
            source_ids=(activity.id, cognition.id, *scene.source_ids),
        )
    )

    candidate = candidate_payload(request, answer, scene)
    if candidate is not None:
        repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="introspection_candidate",
                content=candidate,
                source_ids=(activity.id, cognition.id, *scene.source_ids),
            )
        )

    activity_close.close(
        subject_id,
        activity.id,
        final_response_statuses=(),
        action_id="",
        reason="introspection_finished",
    )
    return IntrospectionRun(
        request=request,
        scene_source_ids=scene.source_ids,
        answer=answer,
        activity_id=activity.id,
        cognitive_content_id=cognition.id,
        status=IntrospectionStatus.DONE,
        duration_ms=duration_ms,
        model_tag=model_tag,
        reason=answer.mode,
    )


def candidate_payload(
    request: IntrospectionRequest,
    answer: IntrospectionAnswer,
    scene: IntrospectionScene,
) -> dict[str, Any] | None:
    """⑥ / ⑤b / ⑤c 有内容时，写一条**占位候选**（500 与审核未立，只落记录）。"""
    kinds: list[str] = []
    if answer.worth_learning:
        kinds.append("learn")
    if answer.tool_need:
        kinds.append("tool")
    if answer.interaction_need:
        kinds.append("commitment")
    if not kinds:
        return None
    return {
        "entry_ref": request.entry_ref,
        "trigger": request.trigger.value,
        "suggested_kinds": kinds,
        "content": render_answer(answer),
        "worth_learning": answer.worth_learning,
        "tool_need": answer.tool_need,
        "tool_capability": answer.tool_capability,
        "tool_when": answer.tool_when,
        "tool_worth": answer.tool_worth,
        "interaction_need": _plain(answer.interaction_need),
        "source_ids": list(scene.source_ids),
        "note": "占位：不写 08 / 100，不发 intake（见《方案 300》§5.12 / §5.13）",
    }


def render_answer(answer: IntrospectionAnswer) -> str:
    """把六问（⑤ 三个出口）写成可读正文。"""
    if answer.degraded and answer.raw_text:
        body = [
            "（结构化解析失败，以下为原文）",
            answer.raw_text,
        ]
        # 降级也要交代现场结论里已经拿到的部分
        if answer.what:
            body.insert(0, f"① 什么：{answer.what}")
        return "\n".join(body)

    tool = "是" if answer.tool_need else "否"
    if answer.tool_need:
        tool = (
            f"是（能力：{answer.tool_capability or '未写'}；"
            f"条件：{answer.tool_when or '未写'}；"
            f"值不值：{answer.tool_worth or '未写'}）"
        )
    interaction = _plain(answer.interaction_need)
    lines = [
        f"① 什么：{answer.what}",
        f"② 我的选择与结果：{answer.choice_and_result}",
        f"③ 为什么（归因）：{answer.why}",
        f"④ 我是否可以做得更好：{answer.better}",
        f"⑤a 以后我应该怎么做：{answer.next_time}",
        f"⑤b 要不要借助外部工具：{tool}",
        f"⑤c 要不要与某个对象进一步交流：{interaction or '否'}",
        f"⑥ 是否值得记下来并学习：{'是' if answer.worth_learning else '否'}",
    ]
    if answer.candidates:
        lines.append(f"候选：{_plain(list(answer.candidates))}")
    return "\n".join(lines)


def _plain(value: object) -> object:
    """把 Mapping / tuple 之类压成可直接 JSON 落库的形状。"""
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)
