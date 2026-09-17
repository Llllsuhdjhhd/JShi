"""进程内有效性分析：打分入库、规则调档、失败不抛出。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from jshi.memory.strategy import RecallStrategyStore
from jshi.models import MemoryRatings

from .baseline import BaselineStats, MemoryQualityEvaluator, calibrate
from .examples import (
    candidate_from_ratings,
    features_from_rows,
    read_examples,
)
from .memory_analyzer import (
    ANALYZER_NAME,
    ANALYZER_VERSION,
    heuristic_strategy,
    should_run,
    suggest_level,
)
from .port import EffectivenessReport, new_id, utc_now
from .ratings import JsonlRatingStore, row_from_ratings
from .strategy import strategy_to_report

logger = logging.getLogger(__name__)


class InProcessEffectiveness:
    def __init__(
        self,
        *,
        ratings: JsonlRatingStore | None = None,
        strategy: RecallStrategyStore | None = None,
        reports_path: Path | str | None = None,
        baseline: BaselineStats | None = None,
        examples_path: Path | str | None = None,
        candidate_path: Path | str | None = None,
    ) -> None:
        self.ratings = ratings or JsonlRatingStore()
        self.strategy = strategy or RecallStrategyStore()
        self._reports_path = Path(reports_path) if reports_path is not None else None
        self._candidate_path = Path(candidate_path) if candidate_path is not None else None
        self._reports: list[EffectivenessReport] = []
        self._last_report_at: dict[str, datetime] = {}
        # 有基准则用它出策略；无基准回退启发式（见 memory_analyzer.heuristic_strategy）。
        if baseline is None and examples_path is not None:
            baseline = calibrate(read_examples(examples_path))
        self._baseline = baseline
        self._evaluator = MemoryQualityEvaluator(baseline) if baseline is not None else None

    def record_ratings(
        self,
        subject_id: str,
        activity_id: str,
        ratings: MemoryRatings,
    ) -> None:
        if not ratings.has_content():
            return
        self.ratings.append(row_from_ratings(subject_id, activity_id, ratings))
        if self._candidate_path is not None:
            self._write_candidate(subject_id, activity_id, ratings)

    def _write_candidate(
        self, subject_id: str, activity_id: str, ratings: MemoryRatings
    ) -> None:
        example = candidate_from_ratings(subject_id, activity_id, ratings)
        self._candidate_path.parent.mkdir(parents=True, exist_ok=True)
        with self._candidate_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(example.to_dict(), ensure_ascii=False) + "\n")

    def pending_gap_query(self, subject_id: str) -> str:
        row = self.ratings.pending_gap(subject_id)
        if row is None:
            return ""
        return (row.gap_query or "").strip()

    def consume_gap(self, subject_id: str) -> None:
        self.ratings.consume_gap(subject_id)

    def reports(self, subject_id: str | None = None) -> tuple[EffectivenessReport, ...]:
        rows = self._reports
        if subject_id is not None:
            rows = [row for row in rows if row.subject_id == subject_id]
        return tuple(rows)

    def run_due(self, subject_id: str) -> None:
        try:
            self._run(subject_id)
        except Exception:
            logger.exception("effectiveness run_due failed")

    def _run(self, subject_id: str) -> None:
        unanalyzed = [
            row
            for row in self.ratings.list(subject_id)
            if not row.analyzed
        ]
        now = utc_now()
        if not should_run(
            unanalyzed,
            last_report_at=self._last_report_at.get(subject_id),
            now=now,
        ):
            return
        current_level = self.strategy.get(subject_id).default_level
        # 用与示例库一致的特征定义归约整批材料，再做分位对比（或无基准兜底）。
        if self._evaluator is not None:
            strategy = self._evaluator.evaluate(
                features_from_rows(unanalyzed), current_level
            )
        else:
            strategy = heuristic_strategy(unanalyzed, current_level)

        report = EffectivenessReport(
            report_id=new_id(),
            subject_id=subject_id,
            analyzer=ANALYZER_NAME,
            created_at=now,
            materials_ref=f"ratings:{len(unanalyzed)}",
            findings={
                "features": features_from_rows(unanalyzed).to_dict(),
                "mode": strategy.recall_mode,
                "confidence": strategy.confidence,
                "reason": strategy.reason,
                "evidence": [dict(entry) for entry in strategy.evidence],
            },
            strategy=strategy_to_report(strategy),
            model_tag="",
            analyzer_version=ANALYZER_VERSION,
        )
        self._reports.append(report)
        self._last_report_at[subject_id] = now
        # 落地可执行参数到 09 薄壳；策略全量留在报告（含 summary_level / recall_mode / basis）。
        self.strategy.apply(
            subject_id,
            default_level=strategy.default_level,
            limit=strategy.limit,
            source_report_id=report.report_id,
        )
        self.ratings.mark_analyzed(unanalyzed)
        self._append_report(report)

    def _append_report(self, report: EffectivenessReport) -> None:
        if self._reports_path is None:
            return
        self._reports_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "report_id": report.report_id,
            "subject_id": report.subject_id,
            "analyzer": report.analyzer,
            "created_at": report.created_at.isoformat(),
            "materials_ref": report.materials_ref,
            "findings": dict(report.findings),
            "strategy": dict(report.strategy),
            "model_tag": report.model_tag,
            "analyzer_version": report.analyzer_version,
        }
        with self._reports_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
