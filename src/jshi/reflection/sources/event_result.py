"""探测器：事件结果（工具没做成）。

弱信号，如实写清：

- ``mode == "think"`` **不作信号**——cognition 提示词里它是"没有人期待你开口"的正常模式
  （见《方案 300》§3.2 / §5.14）；
- ``ToolResult.ideal`` 的默认值就是 ``False``（``tool/contract.py``），所以单看它会把没设过
  这个字段的结果当成失败 → 主判用 ``ToolStatus`` 非 ``ok``，``ideal`` 只在带 ``ideal_note``
  时作辅助；
- "结果好不好"往往要**下一轮**（对方的反应）才知道 → 结果类自省接受"滞后一拍"
  （设计 §11-C21），跨拍回看留到下一刀。
"""

from __future__ import annotations

from ..port import (
    MOMENT_AFTER_ACTIVITY,
    IntrospectionLevel,
    IntrospectionRequest,
    IntrospectionTrigger,
    IntrospectionUrgency,
    SourceContext,
)


class EventResultSource:
    """一次活动收尾后，看这一轮的工具结果有没有没做成。"""

    name = "event_result"
    moments = frozenset({MOMENT_AFTER_ACTIVITY})

    def detect(self, context: SourceContext) -> tuple[IntrospectionRequest, ...]:
        activity = context.activity
        if activity is None or not context.object_id or context.hang_store is None:
            return ()
        failures = _tool_failures(context)
        if not failures:
            return ()
        return (
            IntrospectionRequest(
                subject_id=context.subject_id,
                trigger=IntrospectionTrigger.EVENT_RESULT,
                entry_ref=f"activity:{activity.id}",
                entry_at=getattr(activity, "updated_at", None) or context.now,
                entry_text=_entry_text(context, failures),
                level=IntrospectionLevel.MEDIUM,
                urgency=IntrospectionUrgency.SOON,
                object_id=context.object_id,
                evidence={"tool_failures": failures},
            ),
        )


def _tool_failures(context: SourceContext) -> list[dict[str, object]]:
    """找这次活动名下"没做成"的工具结果。"""
    try:
        records = context.hang_store.list_for(context.subject_id, context.object_id)
    except Exception:  # 记挂读失败不该影响触发判定
        return []
    activity_id = getattr(context.activity, "id", "")
    failures: list[dict[str, object]] = []
    for record in records:
        field_ref = dict(getattr(record, "field_ref", None) or {})
        if field_ref.get("activity_id") != activity_id:
            continue
        for feedback in getattr(record, "feedback", ()) or ():
            result = getattr(feedback, "result", None)
            if result is None:
                continue
            status = getattr(result.status, "value", str(result.status))
            ideal = bool(getattr(result, "ideal", False))
            note = str(getattr(result, "ideal_note", "") or "")
            if status == "ok" and (ideal or not note):
                continue
            failures.append(
                {
                    "status": status,
                    "ideal": ideal,
                    "ideal_note": note,
                    "summary": str(getattr(result, "summary", "") or ""),
                }
            )
    return failures


def _entry_text(context: SourceContext, failures: list[dict[str, object]]) -> str:
    parts: list[str] = []
    reason = str(getattr(context.plan, "reason", "") or "").strip()
    if reason:
        parts.append(reason)
    for failure in failures:
        summary = str(failure.get("summary") or "").strip()
        if summary and summary not in parts:
            parts.append(summary)
    if not parts:
        parts.append(f"活动 {getattr(context.activity, 'id', '')}")
    return "；".join(parts)
