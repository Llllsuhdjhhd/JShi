"""探测器：闲时回顾。

占位、确定性（不用纯随机，见设计 §11-B6/B5）：
在**最近 K 段**（``INTROSPECTION_IDLE_WINDOW``）里挑"最早一段尚未自省过的段"。
限定 K 是为了不让闲时从最老开始"翻旧账"。

选材偏置（遗忘因子 / 情绪唤醒 / 久未回顾）留到下一刀（设计 §11-B6）。
"""

from __future__ import annotations

from ..port import (
    MOMENT_IDLE,
    IntrospectionLevel,
    IntrospectionRequest,
    IntrospectionTrigger,
    IntrospectionUrgency,
    SourceContext,
)
from ..scene import segment_text

_IDLE_HISTORY_SCAN = 200
_INTROSPECTED_EVENTS = frozenset({"introspection_queued", "introspection_done"})


class IdleSource:
    name = "idle"
    moments = frozenset({MOMENT_IDLE})

    def detect(self, context: SourceContext) -> tuple[IntrospectionRequest, ...]:
        segments = list(context.ledger.list_experiences(context.subject_id))
        if not segments:
            return ()
        from jshi.core.params import INTROSPECTION_IDLE_WINDOW

        window = (
            segments[-INTROSPECTION_IDLE_WINDOW:]
            if INTROSPECTION_IDLE_WINDOW > 0
            else segments
        )
        seen = _introspected_refs(context)
        for segment in window:  # 账本按 sequence 升序 → 最早优先
            entry_ref = f"segment:{segment.segment_id}"
            if entry_ref in seen:
                continue
            return (
                IntrospectionRequest(
                    subject_id=context.subject_id,
                    trigger=IntrospectionTrigger.IDLE,
                    entry_ref=entry_ref,
                    entry_at=segment.occurred_at,
                    entry_text=segment_text(segment),
                    level=IntrospectionLevel.LOW,
                    urgency=IntrospectionUrgency.SLOW,
                    object_id=segment.actor_object_id,
                ),
            )
        return ()


def _introspected_refs(context: SourceContext) -> set[str]:
    refs: set[str] = set()
    for record in context.repository.list_history(
        context.subject_id, limit=_IDLE_HISTORY_SCAN
    ):
        if record.event_type not in _INTROSPECTED_EVENTS:
            continue
        ref = str(dict(record.content or {}).get("entry_ref", ""))
        if ref:
            refs.add(ref)
    return refs
