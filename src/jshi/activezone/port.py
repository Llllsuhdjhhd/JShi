from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from jshi.experienceledger import ExperienceLedgerPort, ExperienceSegment, ConsumerKind


# 活跃区长度：占位字符数；未来可由 14 按模型上下文 token 动态调整。
ACTIVE_ZONE_DEFAULT_CHARS = 2000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ActiveZoneView:
    """阶段② 装载结果：原始活动窗口，不含事件概念。"""

    segments: tuple[ExperienceSegment, ...]
    start_sequence: int
    budget_chars: int
    loaded_at: datetime = field(default_factory=utc_now)


class ActiveZonePort(Protocol):
    """活跃区系统：只取 ExperienceLedger 的原始活动窗口并控制长度。"""

    def load(
        self,
        subject_id: str,
        input_text: str,
        *,
        limit_chars: int | None = None,
    ) -> ActiveZoneView:
        """返回从 active_zone_start 开始的原始活动窗口。"""

    def advance(
        self,
        subject_id: str,
        through_sequence: int,
    ) -> int:
        """阶段⑦ 推进活跃区窗口起点。"""


class InProcessActiveZone:
    """最小实现：直接消费 ExperienceLedger，不做事件识别与筛选。"""

    name = "in-process-active-zone"

    def __init__(
        self,
        ledger: ExperienceLedgerPort,
        *,
        default_chars: int = ACTIVE_ZONE_DEFAULT_CHARS,
    ) -> None:
        self._ledger = ledger
        self._default_chars = default_chars

    def load(
        self,
        subject_id: str,
        input_text: str,
        *,
        limit_chars: int | None = None,
    ) -> ActiveZoneView:
        del input_text
        budget = limit_chars if limit_chars is not None else self._default_chars
        segments = self._ledger.active_window(
            subject_id,
            limit_chars=budget,
        )
        start = self._ledger.consumer_cursor(
            subject_id,
            ConsumerKind.ACTIVE_ZONE,
        )
        return ActiveZoneView(
            segments=segments,
            start_sequence=start,
            budget_chars=budget,
        )

    def advance(
        self,
        subject_id: str,
        through_sequence: int,
    ) -> int:
        return self._ledger.advance_consumer_cursor(
            subject_id,
            ConsumerKind.ACTIVE_ZONE,
            through_sequence,
        )
