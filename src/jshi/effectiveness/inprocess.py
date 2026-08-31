"""进程内有效性分析：打分入库、规则调档、失败不抛出。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from jshi.memory.strategy import RecallStrategyStore
from jshi.models import MemoryRatings

from .memory_analyzer import (
    ANALYZER_NAME,
    ANALYZER_VERSION,
    should_run,
    suggest_level,
)
from .port import EffectivenessReport, new_id, utc_now
from .ratings import JsonlRatingStore, row_from_ratings

logger = logging.getLogger(__name__)


class InProcessEffectiveness:
    def __init__(
        self,
        *,
        ratings: JsonlRatingStore | None = None,
        strategy: RecallStrategyStore | None = None,
        reports_path: Path | str | None = None,
    ) -> None:
        self.ratings = ratings or JsonlRatingStore()
        self.strategy = strategy or RecallStrategyStore()
        self._reports_path = Path(reports_path) if reports_path is not None else None
        self._reports: list[EffectivenessReport] = []
        self._last_report_at: dict[str, datetime] = {}

    def record_ratings(
        self,
        subject_id: str,
        activity_id: str,
        ratings: MemoryRatings,
    ) -> None:
        if not ratings.has_content():
            return
        self.ratings.append(row_from_ratings(subject_id, activity_id, ratings))

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
        current = self.strategy.get(subject_id).default_level
        level, findings = suggest_level(unanalyzed, current)
        report = EffectivenessReport(
            report_id=new_id(),
            subject_id=subject_id,
            analyzer=ANALYZER_NAME,
            created_at=now,
            materials_ref=f"ratings:{len(unanalyzed)}",
            findings=dict(findings),
            strategy={"default_level": level, "limit": None},
            model_tag="",
            analyzer_version=ANALYZER_VERSION,
        )
        self._reports.append(report)
        self._last_report_at[subject_id] = now
        self.strategy.apply(
            subject_id,
            default_level=level,
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
