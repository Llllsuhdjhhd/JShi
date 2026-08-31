"""记忆质量分析器：规则汇总，本期不调大模型。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping

from .ratings import RatingRow

ANALYZER_NAME = "memory_quality"
ANALYZER_VERSION = "rules-v1"
MIN_UNANALYZED_ROWS = 8
REPORT_INTERVAL = timedelta(hours=24)
UNRELATED_RATIO = 0.5
MISSING_COVERAGE_COUNT = 3


def should_run(
    unanalyzed: list[RatingRow],
    *,
    last_report_at: datetime | None,
    now: datetime,
) -> bool:
    if len(unanalyzed) >= MIN_UNANALYZED_ROWS:
        return True
    if not unanalyzed:
        return False
    oldest = min(row.created_at for row in unanalyzed)
    if last_report_at is None:
        return now - oldest >= REPORT_INTERVAL
    return now - last_report_at >= REPORT_INTERVAL


def suggest_level(
    unanalyzed: list[RatingRow],
    current_level: int,
) -> tuple[int, Mapping[str, object]]:
    items = [item for row in unanalyzed for item in row.items]
    unrelated = sum(1 for item in items if item.get("relevance") == "unrelated")
    missing = sum(1 for row in unanalyzed if row.coverage == "missing")
    level = current_level
    findings: dict[str, object] = {
        "rated_items": len(items),
        "unrelated": unrelated,
        "missing_coverage": missing,
    }
    if items and unrelated / len(items) >= UNRELATED_RATIO:
        level -= 1
        findings["level_delta_unrelated"] = -1
    if missing >= MISSING_COVERAGE_COUNT:
        level += 1
        findings["level_delta_missing"] = 1
    return min(max(level, 1), 9), findings
