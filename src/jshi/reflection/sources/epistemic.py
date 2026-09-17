"""探测器：认识被修正 / 驳回。

**如实标注**：``SubjectProcess.transition_cognition`` 现在只有 CLI 与测试在调
（主流程没有自动调用方），所以这个源是「**人工迁移 → 自省**」，
**不是**"自动发现结果异常"（设计 §11-C20）。它补的是：人工标注"这条想错了"之后，
自省自动接着做一轮（为什么错、以后怎么判）。

现场取**两个时间中心**：认识形成时（看当时的依据）+ 迁移发生时（看为什么改）。
"""

from __future__ import annotations

from ..port import (
    MOMENT_AFTER_ACTIVITY,
    MOMENT_IDLE,
    IntrospectionLevel,
    IntrospectionRequest,
    IntrospectionTrigger,
    IntrospectionUrgency,
    SourceContext,
)


class EpistemicRevisedSource:
    name = "epistemic_revised"
    moments = frozenset({MOMENT_AFTER_ACTIVITY, MOMENT_IDLE})
    history_scan = 50
    target_states = frozenset({"revised", "rejected"})

    def detect(self, context: SourceContext) -> tuple[IntrospectionRequest, ...]:
        found: list[IntrospectionRequest] = []
        history = context.repository.list_history(
            context.subject_id, limit=self.history_scan
        )
        for record in history:
            if record.event_type != "epistemic_transition":
                continue
            content = dict(record.content or {})
            to_state = str(content.get("to", ""))
            if to_state not in self.target_states:
                continue
            content_id = str(content.get("cognitive_content_id", ""))
            if not content_id:
                continue
            cognition = _load_cognition(context, content_id)
            if cognition is None:
                continue
            found.append(
                IntrospectionRequest(
                    subject_id=context.subject_id,
                    trigger=IntrospectionTrigger.EPISTEMIC_REVISED,
                    entry_ref=f"cognition:{content_id}",
                    entry_at=record.created_at,  # 迁移发生时
                    entry_at_also=getattr(cognition, "created_at", None),  # 认识形成时
                    entry_text=str(getattr(cognition, "content", "") or ""),
                    level=IntrospectionLevel.MEDIUM,
                    urgency=IntrospectionUrgency.SOON,
                    object_id=context.object_id,
                    evidence={
                        "from": str(content.get("from", "")),
                        "to": to_state,
                        "reason": str(content.get("reason", "")),
                    },
                    origin="manual_transition",
                )
            )
        return tuple(found)


def _load_cognition(context: SourceContext, content_id: str):
    try:
        return context.repository.get_cognitive_content(content_id)
    except (KeyError, ValueError):
        return None
