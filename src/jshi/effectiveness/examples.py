"""有效性分析示例库：带标注的召回案例，作为“评价基准”。

每个例子记录一次召回场景的**特征**（与运行时同一定义），加一个**真值标签**和
**真值策略**，作为校准基准。运行时把当前打分换算成同样的特征，与基准分布对比，
借此给出策略建议（而不是凭空一个分数）。

测量基准（``RatingFeatures``）刻意与运行时共用：先由
``features_from_ratings`` / ``features_from_rows`` 把原始打分归约成可比信号，
再在 ``baseline`` 模块里由示例库校准出参考分位/阈值。分数只用于后续校准与
报告中的“证据”，不是本系统对外输出的策略。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from jshi.models import MemoryRatings

from .strategy import MemoryRecallStrategy, utc_now

SCHEMA_VERSION = "ratings-example-v1"

_COVERAGE_DEFAULT = {"": 0.0, "sufficient": 0.2, "thin": 0.6, "missing": 1.0}


@dataclass(frozen=True)
class RatingFeatures:
    """把一轮（或若干轮）现场打分归约而成的可比信号。

    ``precision_failure`` / ``coverage_failure`` 是两条复合“失败”信号（0..1，越大
    越差）；``quality_score`` 只是它们的综合（供校准与证据），**不是输出策略**。
    """

    rated_count: int = 0
    unrelated_ratio: float = 0.0
    misleading_ratio: float = 0.0
    redundant_ratio: float = 0.0
    helps_avg: float = 0.0  # 0..2
    coverage: str = ""  # "" | sufficient | thin | missing
    has_gap: bool = False
    precision_failure: float = 0.0  # 0..1：召回里“垃圾/低相关”的强度
    coverage_failure: float = 0.0  # 0..1：召回“不够/缺人”的强度
    quality_score: float = 1.0  # 0..1：1 - max(precision, coverage) 失败；仅作证据

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RatingFeatures:
        def _f(key: str, default: float = 0.0) -> float:
            try:
                return float(payload.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            rated_count=int(payload.get("rated_count", 0) or 0),
            unrelated_ratio=_f("unrelated_ratio"),
            misleading_ratio=_f("misleading_ratio"),
            redundant_ratio=_f("redundant_ratio"),
            helps_avg=_f("helps_avg"),
            coverage=str(payload.get("coverage") or ""),
            has_gap=bool(payload.get("has_gap", False)),
            precision_failure=_f("precision_failure"),
            coverage_failure=_f("coverage_failure"),
            quality_score=_f("quality_score", 1.0),
        )


def _ratio(count: int, total: int) -> float:
    return count / total if total else 0.0


def _clean_coverage(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in _COVERAGE_DEFAULT else ""


def features_from_ratings(ratings: MemoryRatings) -> RatingFeatures:
    """由单轮 ``MemoryRatings`` 归约成可比信号（运行时与示例库共用）。"""
    items = ratings.items or ()
    total = len(items)
    unrelated = sum(1 for item in items if item.relevance == "unrelated")
    misleading = sum(1 for item in items if item.misleading)
    redundant = sum(1 for item in items if item.redundant)
    helps_sum = sum(int(item.helps_understanding) for item in items)

    unrelated_ratio = _ratio(unrelated, total)
    misleading_ratio = _ratio(misleading, total)
    redundant_ratio = _ratio(redundant, total)
    helps_avg = (helps_sum / total) if total else 0.0

    # 复合“失败”信号：各分量 0..1，等权平均。阈值/方向由示例库校准（见 baseline），
    # 这里是统一测量单位，不写死“无关占比过半”这类魔法数。
    precision_failure = _mean_of(
        unrelated_ratio,
        misleading_ratio,
        redundant_ratio,
        # 帮助度越低越说明召回无关紧要；无在场回忆时归零，避免虚高。
        ((2.0 - helps_avg) / 2.0) if total else 0.0,
    )
    coverage_failure = _COVERAGE_DEFAULT[_clean_coverage(ratings.coverage)]
    has_gap = bool((ratings.gap_query or "").strip())
    if has_gap:
        coverage_failure = max(coverage_failure, 0.7)

    quality_score = max(0.0, min(1.0, 1.0 - max(precision_failure, coverage_failure)))
    return RatingFeatures(
        rated_count=total,
        unrelated_ratio=round(unrelated_ratio, 4),
        misleading_ratio=round(misleading_ratio, 4),
        redundant_ratio=round(redundant_ratio, 4),
        helps_avg=round(helps_avg, 4),
        coverage=_clean_coverage(ratings.coverage),
        has_gap=has_gap,
        precision_failure=round(precision_failure, 4),
        coverage_failure=round(coverage_failure, 4),
        quality_score=round(quality_score, 4),
    )


def _mean_of(*values: float) -> float:
    return sum(values) / len(values) if values else 0.0


def features_from_rows(rows: Sequence[object]) -> RatingFeatures:
    """把若干未分析打分行聚合成一个可比信号（供分析器在整批材料上推导）。

    逐条比例按“全部被打分条目”计；coverage 取最差；任一含 gap 即视为有缺口。
    """
    items = [item for row in rows for item in getattr(row, "items", ()) or ()]
    total = len(items)
    unrelated = sum(1 for item in items if item.get("relevance") == "unrelated")
    misleading = sum(1 for item in items if item.get("misleading"))
    redundant = sum(1 for item in items if item.get("redundant"))
    helps = [int(item.get("helps_understanding", 0)) for item in items]

    coverages = [_clean_coverage(getattr(row, "coverage", "") or "") for row in rows]
    worst = "missing" if "missing" in coverages else (
        "thin" if "thin" in coverages else (
            "sufficient" if coverages and "sufficient" in coverages else ""
        )
    )
    has_gap = any(bool((getattr(row, "gap_query", "") or "").strip()) for row in rows)

    unrelated_ratio = _ratio(unrelated, total)
    misleading_ratio = _ratio(misleading, total)
    redundant_ratio = _ratio(redundant, total)
    helps_avg = (sum(helps) / total) if total else 0.0
    precision_failure = _mean_of(
        unrelated_ratio,
        misleading_ratio,
        redundant_ratio,
        ((2.0 - helps_avg) / 2.0) if total else 0.0,
    )
    coverage_failure = _COVERAGE_DEFAULT[worst]
    if has_gap:
        coverage_failure = max(coverage_failure, 0.7)
    quality_score = max(0.0, min(1.0, 1.0 - max(precision_failure, coverage_failure)))

    return RatingFeatures(
        rated_count=total,
        unrelated_ratio=round(unrelated_ratio, 4),
        misleading_ratio=round(misleading_ratio, 4),
        redundant_ratio=round(redundant_ratio, 4),
        helps_avg=round(helps_avg, 4),
        coverage=worst,
        has_gap=has_gap,
        precision_failure=round(precision_failure, 4),
        coverage_failure=round(coverage_failure, 4),
        quality_score=round(quality_score, 4),
    )


@dataclass(frozen=True)
class RatingExample:
    """一条带标注的召回案例，作为评价基准。

    标注 = 这个场景被判定为 ``good`` 还是 ``poor``，以及真值该给什么策略
    （``truth_strategy``）；``reason`` 说明为何这样标。运行时在示例库上校准，
    再把当前观测与之对比。
    """

    schema_version: str = SCHEMA_VERSION
    example_id: str = ""
    subject_id: str = ""
    input_text: str = ""
    speaker: str = ""
    in_effect_level: int = 1  # 该案例被抓取时的召回档位
    context: Mapping[str, Any] = field(default_factory=dict)
    features: RatingFeatures = field(default_factory=RatingFeatures)
    quality_label: str = "good"  # good | poor
    truth_strategy: MemoryRecallStrategy = field(default_factory=MemoryRecallStrategy)
    reason: str = ""
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "example_id": self.example_id,
            "subject_id": self.subject_id,
            "input_text": self.input_text,
            "speaker": self.speaker,
            "in_effect_level": self.in_effect_level,
            "context": dict(self.context),
            "features": self.features.to_dict(),
            "quality_label": self.quality_label,
            "truth_strategy": self.truth_strategy.to_dict(),
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
        }


def read_examples(path: Path | str) -> tuple[RatingExample, ...]:
    path = Path(path)
    if not path.exists():
        return ()
    examples: list[RatingExample] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        examples.append(example_from_dict(payload))
    return tuple(examples)


def write_examples(path: Path | str, examples: Sequence[RatingExample]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        json.dumps(example.to_dict(), ensure_ascii=False) + "\n" for example in examples
    )
    path.write_text(body, encoding="utf-8")


def candidate_from_ratings(
    subject_id: str,
    activity_id: str,
    ratings: MemoryRatings,
) -> RatingExample:
    """把一轮未标注的现场打分收集成“候选例子”（供离线标注成基准）。

    候选一开始不带真值：``quality_label="unlabeled"``、``truth_strategy`` 为空。
    离线人工（或治理）标注成 ``good`` / ``poor`` 并给出 ``truth_strategy`` 后，
    这份文件即可作为示例库基准（``examples_path``）。
    """
    return RatingExample(
        subject_id=subject_id,
        context={"activity_id": activity_id},
        features=features_from_ratings(ratings),
        quality_label="unlabeled",
        truth_strategy=MemoryRecallStrategy(recall_mode="balanced"),
        reason="待标注",
    )


def example_from_dict(payload: Mapping[str, Any]) -> RatingExample:
    raw_features = payload.get("features") or {}
    raw_strategy = payload.get("truth_strategy") or {}
    created = payload.get("created_at") or ""
    try:
        created_at = datetime.fromisoformat(created) if created else utc_now()
    except ValueError:
        created_at = utc_now()
    return RatingExample(
        schema_version=str(payload.get("schema_version") or SCHEMA_VERSION),
        example_id=str(payload.get("example_id") or ""),
        subject_id=str(payload.get("subject_id") or ""),
        input_text=str(payload.get("input_text") or ""),
        speaker=str(payload.get("speaker") or ""),
        in_effect_level=int(payload.get("in_effect_level", 1) or 1),
        context=dict(payload.get("context") or {}),
        features=RatingFeatures.from_dict(raw_features),
        quality_label=str(payload.get("quality_label") or "good"),
        truth_strategy=strategy_from_dict(raw_strategy),
        reason=str(payload.get("reason") or ""),
        created_at=created_at,
    )


def strategy_from_dict(payload: Mapping[str, Any]) -> MemoryRecallStrategy:
    updated = payload.get("updated_at") or ""
    try:
        updated_at = datetime.fromisoformat(updated) if updated else None
    except ValueError:
        updated_at = None
    evidence = tuple(dict(item) for item in (payload.get("evidence") or ()))
    return MemoryRecallStrategy(
        default_level=int(payload.get("default_level", 1) or 1),
        limit=(int(payload["limit"]) if payload.get("limit") is not None else None),
        summary_level=payload.get("summary_level"),
        recall_mode=str(payload.get("recall_mode") or "balanced"),
        confidence=float(payload.get("confidence", 0.0) or 0.0),
        reason=str(payload.get("reason") or ""),
        evidence=evidence,
        basis=str(payload.get("basis") or "heuristic"),
        source_report_id=str(payload.get("source_report_id") or ""),
        updated_at=updated_at,
    )
