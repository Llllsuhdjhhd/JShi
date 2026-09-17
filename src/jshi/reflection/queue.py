"""自省队列：入队、主题锁 / 冷却、上限、按时效排程。

- **主题锁 / 冷却**：同一 ``theme`` 在冷却期内不再入队；同一主题的**同级或更低**请求视为重复；
  只有"更急或更深"的才放行——这样"急的浅一轮 + 缓的深一轮"能走通（设计 §3.2）。
- **上限**：队列满了丢最"缓"的一条（同级丢最旧）；来的那条更缓就直接丢它。
- **排程**：急 > 尽快 > 缓，同级按触发时间先来先做（设计 §3.4）。
- 锁与冷却都在内存：进程重启后冷却失效（方案 §5.7，落盘留到后面一刀）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from jshi.core.params import (
    INTROSPECTION_COOLDOWN_SECONDS,
    INTROSPECTION_MAX_QUEUE,
)

from .port import (
    IntrospectionRequest,
    IntrospectionStatus,
    level_rank,
    urgency_rank,
    utc_now,
)


@dataclass(frozen=True)
class EnqueueResult:
    """入队结果；``evicted`` 是被挤出去的那条（调用方负责记 dropped）。"""

    status: IntrospectionStatus
    reason: str = ""
    evicted: IntrospectionRequest | None = None


class IntrospectionQueue:
    def __init__(
        self,
        *,
        max_size: int = INTROSPECTION_MAX_QUEUE,
        cooldown_seconds: int = INTROSPECTION_COOLDOWN_SECONDS,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._max_size = max(1, int(max_size))
        self._cooldown_seconds = max(0, int(cooldown_seconds))
        self._now = now
        self._pending: list[IntrospectionRequest] = []
        self._cooling: dict[str, datetime] = {}
        self._seen: dict[str, tuple[int, int]] = {}

    # -- 入口 ---------------------------------------------------------------

    def enqueue(self, request: IntrospectionRequest) -> EnqueueResult:
        theme = request.theme
        if self._cooling_until(theme) is not None:
            return EnqueueResult(IntrospectionStatus.DROPPED, "cooldown")

        rank = (level_rank(request.level), urgency_rank(request.urgency))
        previous = self._seen.get(theme)
        if previous is not None and rank <= previous:
            return EnqueueResult(IntrospectionStatus.DROPPED, "duplicate")

        evicted: IntrospectionRequest | None = None
        if len(self._pending) >= self._max_size:
            worst = min(self._pending, key=_schedule_key)
            if urgency_rank(request.urgency) <= urgency_rank(worst.urgency):
                return EnqueueResult(IntrospectionStatus.DROPPED, "queue_full")
            self._pending.remove(worst)
            evicted = worst

        self._pending.append(request)
        self._seen[theme] = rank
        return EnqueueResult(IntrospectionStatus.QUEUED, evicted=evicted)

    # -- 排程 ---------------------------------------------------------------

    def pop_next(self, subject_id: str | None = None) -> IntrospectionRequest | None:
        pool = [
            item
            for item in self._pending
            if subject_id is None or item.subject_id == subject_id
        ]
        if not pool:
            return None
        chosen = min(pool, key=_schedule_key)
        self._pending.remove(chosen)
        return chosen

    def finish(self, request: IntrospectionRequest) -> None:
        """跑完（或被丢）后写冷却。"""
        if self._cooldown_seconds <= 0:
            return
        self._cooling[request.theme] = self._now() + timedelta(
            seconds=self._cooldown_seconds
        )

    # -- 观测 ---------------------------------------------------------------

    def pending(self) -> tuple[IntrospectionRequest, ...]:
        return tuple(self._pending)

    def __len__(self) -> int:
        return len(self._pending)

    def _cooling_until(self, theme: str) -> datetime | None:
        until = self._cooling.get(theme)
        if until is None:
            return None
        if until <= self._now():
            self._cooling.pop(theme, None)
            return None
        return until


def _schedule_key(request: IntrospectionRequest) -> tuple[int, datetime]:
    """排程键：急优先，其次先来先做。"""
    return (-urgency_rank(request.urgency), request.created_at)
