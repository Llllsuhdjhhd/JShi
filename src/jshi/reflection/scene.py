"""现场（设计 §4.1）：自省看见什么。

只读，三份材料拼成现场：

```
① 当时的上下文   ┐
② 前后现场       ├ 账本时间窗：按时间取（不按段数），取 [中心 − Δ, 中心 + Δ] 内的经历段
③ 09 回溯召回    ┘ 拿条目文本去 09 召回，补背景与类似经历
```

- "当时"= 被考察条目发生的时间段；``entry_at_also`` 给第二个中心（如"迁移发生时"）；
- 段取 ``accepted`` / ``consumed``（已收尾），不取 ``archived``；
- 材料不足不硬编：返回 ``None``，由调用方记 ``dropped(no_material)``；
- 不改写账本、不写活跃区、不改个人世界。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from jshi.core.params import (
    INTROSPECTION_RECALL_LIMIT,
    INTROSPECTION_SCENE_MAX_SEGMENTS,
    INTROSPECTION_SCENE_WINDOW_SECONDS,
)
from jshi.experienceledger.port import SegmentStatus

from .port import IntrospectionRequest, IntrospectionScene

_USABLE_STATUSES = (SegmentStatus.ACCEPTED, SegmentStatus.CONSUMED)


def segment_text(segment: object) -> str:
    """取一段经历的可读正文：原文优先，其次内部状态摘要。"""
    text = str(getattr(segment, "text_raw", "") or "").strip()
    if text:
        return text
    state = getattr(segment, "state_delta", None)
    if state:
        return _brief(state)
    plan = getattr(segment, "response_plan", None)
    if plan:
        return _brief(plan)
    return ""


def build_scene(
    request: IntrospectionRequest,
    *,
    ledger: object,
    memory: object,
    window_seconds: int = INTROSPECTION_SCENE_WINDOW_SECONDS,
    max_segments: int = INTROSPECTION_SCENE_MAX_SEGMENTS,
    recall_limit: int = INTROSPECTION_RECALL_LIMIT,
) -> IntrospectionScene | None:
    """按时间窗取段 + 召回一次，拼成现场；没材料返回 ``None``。"""
    centers = request.centers
    delta = timedelta(seconds=max(1, int(window_seconds)))
    try:
        all_segments = list(ledger.list_experiences(request.subject_id))
    except Exception:
        all_segments = []

    picked = [
        segment
        for segment in all_segments
        if getattr(segment, "status", None) in _USABLE_STATUSES
        and _within(segment, centers, window_seconds)
    ]
    picked.sort(key=lambda item: getattr(item, "sequence", 0))
    if len(picked) > max_segments:
        picked = _nearest(picked, centers, max_segments)

    query = (request.entry_text or "").strip()
    if not query and picked:
        query = segment_text(picked[0])
    recalled: tuple[object, ...] = ()
    if query and memory is not None:
        try:
            recalled = tuple(
                memory.recall(
                    request.subject_id,
                    query,
                    limit=recall_limit,
                    object_id=request.object_id,
                    level=1,
                )
                or ()
            )
        except Exception:
            recalled = ()

    if not picked and not recalled:
        return None

    window = (min(centers) - delta, max(centers) + delta)
    source_ids = tuple(
        dict.fromkeys(
            [
                *(getattr(segment, "segment_id", "") for segment in picked),
                *(getattr(fragment, "event_id", "") for fragment in recalled),
            ]
        )
    )
    source_ids = tuple(item for item in source_ids if item)
    segments = tuple(picked)
    return IntrospectionScene(
        request=request,
        window=window,
        segments=segments,
        recalled=recalled,
        text=render_scene(request, segments, recalled, window),
        source_ids=source_ids,
    )


def render_scene(
    request: IntrospectionRequest,
    segments: tuple[object, ...],
    recalled: tuple[object, ...],
    window: tuple[datetime, datetime],
) -> str:
    """把现场写成给模型看的正文（六问的输入）。"""
    lines = [
        f"【现场｜条目 {request.entry_ref}｜来源 {request.trigger.value}】",
        f"对象：{request.object_id or '主体自己'}",
        f"当时：{_fmt(window[0])} ~ {_fmt(window[1])}",
        "",
        f"— 当时的上下文与前后现场（{len(segments)} 段，按时间）—",
    ]
    if segments:
        for index, segment in enumerate(segments, start=1):
            kind = getattr(getattr(segment, "output_kind", None), "value", "?")
            actor = getattr(segment, "actor_object_id", None) or "系统"
            lines.append(
                f"[{index}] {_fmt(getattr(segment, 'occurred_at', None))} "
                f"{kind}/{actor}：{segment_text(segment)}"
            )
    else:
        lines.append("（这一段没有可用经历）")

    lines.append("")
    lines.append(f"— 回溯（09 召回 {len(recalled)} 条）—")
    if recalled:
        for index, fragment in enumerate(recalled, start=1):
            body = str(
                getattr(fragment, "content", "")
                or getattr(fragment, "text", "")
                or ""
            )
            lines.append(f"[{index}] {body}")
    else:
        lines.append("（没有召回）")
    return "\n".join(lines)


def _within(segment: object, centers: tuple[datetime, ...], window: int) -> bool:
    occurred = getattr(segment, "occurred_at", None)
    if occurred is None:
        return False
    return any(abs((occurred - center).total_seconds()) <= window for center in centers)


def _nearest(
    segments: list[object], centers: tuple[datetime, ...], limit: int
) -> list[object]:
    """段太多时留离时间中心最近的，再按时间还原。"""

    def distance(segment: object) -> float:
        occurred = getattr(segment, "occurred_at", None)
        if occurred is None:
            return float("inf")
        return min(abs((occurred - center).total_seconds()) for center in centers)

    kept = sorted(segments, key=distance)[:limit]
    kept.sort(key=lambda item: getattr(item, "sequence", 0))
    return kept


def _brief(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _fmt(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else "?"
