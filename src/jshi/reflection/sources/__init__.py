"""300 的触发源探测器：集中住这里，各自**只读**别的模块的数据。

信封字段见 ``port.IntrospectionRequest``；三个时刻见 ``port.MOMENT_*``。

本刀实现三个代表性的源：

- ``event_result``：事件结果——工具没做成（弱信号，见《方案 300》§3.2）；
- ``epistemic_revised``：认识被修正 / 驳回（**现为人工迁移触发**，见设计 §11-C20）；
- ``idle``：闲时回顾（最近 K 段里最早未自省过的一段）。

其余源留注册位：边界被触碰 / 承诺到期或被违反 / 同类事件重复 / 对象关系显著变化 / 强烈情绪，
各自缺什么信号写在设计 §11-C16 / C17。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Sequence

from ..port import (
    IntrospectionRequest,
    IntrospectionSource,
    SourceContext,
    level_rank,
    urgency_rank,
)
from .epistemic import EpistemicRevisedSource
from .event_result import EventResultSource
from .idle import IdleSource

__all__ = [
    "EpistemicRevisedSource",
    "EventResultSource",
    "IdleSource",
    "default_sources",
    "detect",
    "merge",
]


def default_sources() -> tuple[IntrospectionSource, ...]:
    """本刀注册的三个探测器；加源只改这里。"""
    return (EventResultSource(), EpistemicRevisedSource(), IdleSource())


def detect(
    sources: Sequence[IntrospectionSource],
    moment: str,
    context: SourceContext,
) -> tuple[IntrospectionRequest, ...]:
    """问一遍响应这个时刻的探测器，然后合并不重复的条目。"""
    found: list[IntrospectionRequest] = []
    for source in sources:
        if moment not in source.moments:
            continue
        found.extend(source.detect(context) or ())
    return merge(found)


def merge(
    requests: Iterable[IntrospectionRequest],
) -> tuple[IntrospectionRequest, ...]:
    """同一条记录合并：一次 pump 撞上多个信号时，只留**一条**自省。

    规则：按 ``theme`` 分组；``evidence`` 合并（原样保留各路信号）；``level`` /
    ``urgency`` 取高；``entry_text`` 取长的那条；``entry_at_also`` 取先到的非空值。
    不合并 → 同一次活动会被开成两条自省，回顾同一件事。
    """
    grouped: dict[str, IntrospectionRequest] = {}
    signals: dict[str, list[dict]] = {}
    for request in requests:
        key = request.theme
        current = grouped.get(key)
        if current is None:
            grouped[key] = request
            signals[key] = [dict(request.evidence)]
            continue
        bucket = signals.setdefault(key, [dict(current.evidence)])
        bucket.append(dict(request.evidence))
        grouped[key] = replace(
            current,
            entry_text=(
                request.entry_text
                if len(request.entry_text or "") > len(current.entry_text or "")
                else current.entry_text
            ),
            level=(
                current.level
                if level_rank(current.level) >= level_rank(request.level)
                else request.level
            ),
            urgency=(
                current.urgency
                if urgency_rank(current.urgency) >= urgency_rank(request.urgency)
                else request.urgency
            ),
            entry_at_also=current.entry_at_also or request.entry_at_also,
            evidence={"merged": list(bucket)},
        )
    return tuple(grouped.values())
