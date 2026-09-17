"""记忆质量分析器：规则汇总，本期不调大模型。

触发条件 ``should_run`` 与旧规则一致；无基准（示例库为空）时用启发式
``heuristic_strategy`` 兜底，有基准时由 ``baseline.MemoryQualityEvaluator``
给出更具依据的策略。分析器只负责“何时跑”与“无基准兜底”，策略本身在
``baseline`` / ``strategy`` 模块。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping

from .ratings import RatingRow
from .strategy import MemoryRecallStrategy, utc_now

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


def heuristic_strategy(
    unanalyzed: list[RatingRow],
    current_level: int,
) -> MemoryRecallStrategy:
    """无基准回退：仍用旧规则定档位，但输出统一为“策略”而非分数。

    有 ``baseline`` 时请让位给 ``baseline.MemoryQualityEvaluator``。
    """
    level, findings = suggest_level(unanalyzed, current_level)
    delta = level - current_level
    mode = "precision" if delta < 0 else ("coverage" if delta > 0 else "balanced")
    if delta < 0:
        reason = "无基准兜底：低相关/无关条目比例过高，降档以换取精度。"
    elif delta > 0:
        reason = "无基准兜底：coverage=missing 次数偏多，升档以补缺口。"
    else:
        reason = "无基准兜底：未触发升/降，档位保持不变。"
    return MemoryRecallStrategy(
        default_level=level,
        recall_mode=mode,
        confidence=0.0,
        reason=reason,
        evidence=(dict(findings),),
        basis="heuristic",
        updated_at=utc_now(),
    )
