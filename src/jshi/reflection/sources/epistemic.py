"""探测器：认识被修正 / 驳回。

**如实标注**：``SubjectProcess.transition_cognition`` 现在只有 CLI 与测试在调
（主流程没有自动调用方），所以这个源是「**人工迁移 → 自省**」，
**不是**"自动发现结果异常"（设计 §11-C20）。它补的是：人工标注"这条想错了"之后，
自省自动接着做一轮（为什么错、以后怎么判）。

现场取**两个时间中心**：认识形成时（看当时的依据）+ 迁移发生时（看为什么改）。
"""

from __future__ import annotations

from jshi.core.params import reflection_param

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
    target_states = frozenset({"revised", "rejected"})

    def detect(self, context: SourceContext) -> tuple[IntrospectionRequest, ...]:
        found: list[IntrospectionRequest] = []
        history = context.repository.list_history(
            context.subject_id, limit=int(reflection_param("INTROSPECTION_TRANSITION_HISTORY_LIMIT"))
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
            found.append(self.from_record(record, cognition))
        return tuple(found)

    @staticmethod
    def from_record(record, cognition):
        content = record.content
        evidence = {key: str(content.get(key, "")) for key in ("from", "to", "reason")}
        if getattr(record, "id", None):
            evidence["transition_ref"] = "transition:" + record.id
        if content.get("segment_id"):
            evidence["segment_ids"] = [content["segment_id"]]
        return IntrospectionRequest(
            subject_id=record.subject_id, trigger=IntrospectionTrigger.EPISTEMIC_REVISED,
            entry_ref=f"cognition:{cognition.id}", entry_at=record.created_at,
            entry_at_also=cognition.created_at, entry_text=cognition.content,
            level=IntrospectionLevel.MEDIUM, urgency=IntrospectionUrgency.SOON,
            evidence=evidence, origin="manual_transition",
        )


def _load_cognition(context: SourceContext, content_id: str):
    try:
        return context.repository.get_cognitive_content(content_id)
    except (KeyError, ValueError):
        return None
