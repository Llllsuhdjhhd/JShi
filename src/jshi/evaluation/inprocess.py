from __future__ import annotations

from datetime import datetime

from .port import EvaluationCollectorPort, EvaluationEvent, EvaluationEventPort


class InProcessEvaluationSystem(EvaluationEventPort, EvaluationCollectorPort):
    """主链路旁路事件收集器。不是有效性分析，不含报告与策略。"""

    def __init__(self) -> None:
        self._events: list[EvaluationEvent] = []

    def emit(self, event: EvaluationEvent) -> None:
        self._events.append(event)

    def collect(
        self,
        subject_id: str | None = None,
        *,
        since: datetime | None = None,
    ) -> tuple[EvaluationEvent, ...]:
        events = self._events
        if subject_id is not None:
            events = [event for event in events if event.subject_id == subject_id]
        if since is not None:
            events = [event for event in events if event.occurred_at >= since]
        return tuple(events)
