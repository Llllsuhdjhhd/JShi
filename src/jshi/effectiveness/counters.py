"""已登记的计数器。观察期用同一个函数、同一段窗口重数。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class CountWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class CountResult:
    count: float
    activity_ids: tuple[str, ...]


def _in_window(moment: datetime, window: CountWindow) -> bool:
    return window.start <= moment <= window.end


def _ids(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def ratings_unrelated_ratio(rows: Sequence[Any], window: CountWindow) -> CountResult:
    chosen = [row for row in rows if _in_window(row.created_at, window)]
    pairs = [(row, item) for row in chosen for item in row.items]
    if not pairs:
        return CountResult(0.0, ())
    hits = [row for row, item in pairs if item.get("relevance") == "unrelated"]
    return CountResult(len(hits) / len(pairs), _ids(row.activity_id for row in hits))


def ratings_missing_coverage(rows: Sequence[Any], window: CountWindow) -> CountResult:
    hits = [
        row
        for row in rows
        if _in_window(row.created_at, window) and row.coverage == "missing"
    ]
    return CountResult(float(len(hits)), _ids(row.activity_id for row in hits))


def ratings_misleading(rows: Sequence[Any], window: CountWindow) -> CountResult:
    hits = [
        row
        for row in rows
        if _in_window(row.created_at, window)
        for item in row.items
        if item.get("misleading")
    ]
    return CountResult(float(len(hits)), _ids(row.activity_id for row in hits))


def traces_empty_recall(rows: Sequence[Any], window: CountWindow) -> CountResult:
    hits = [
        row
        for row in rows
        if _in_window(row.created_at, window)
        and (row.query or "").strip()
        and not row.recalled_event_ids
    ]
    return CountResult(float(len(hits)), _ids(row.activity_id for row in hits))


def timing_total_ms_p90(rows: Sequence[Any], window: CountWindow) -> CountResult:
    chosen = [row for row in rows if _in_window(row.started_at, window)]
    if not chosen:
        return CountResult(0.0, ())
    ordered = sorted(float(row.total_ms) for row in chosen)
    index = min(len(ordered) - 1, max(0, math.ceil(0.9 * len(ordered)) - 1))
    return CountResult(ordered[index], _ids(row.activity_id for row in chosen))


COUNTERS: dict[str, Callable[..., CountResult]] = {
    "ratings.unrelated_ratio": ratings_unrelated_ratio,
    "ratings.missing_coverage": ratings_missing_coverage,
    "ratings.misleading": ratings_misleading,
    "traces.empty_recall": traces_empty_recall,
    "timing.total_ms_p90": timing_total_ms_p90,
}


def run_counter(counter_id: str, rows: Sequence[Any], window: CountWindow) -> CountResult:
    try:
        function = COUNTERS[counter_id]
    except KeyError as exc:
        raise KeyError(counter_id) from exc
    return function(rows, window)
