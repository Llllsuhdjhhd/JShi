"""有效性分析：从示例库校准出“评价基准”，再做分位对比给出策略。

两件事分开：

1. ``calibrate(examples)``：离线（非实时）用带标注的召回案例，求出一套**参考基准**
   ——每条复合失败信号（precision / coverage）在“标注好”案例里的参考分位阈值，
   以及“标注差”案例相对当前档位的典型调整量。这些阈值不再写死魔法数，
   而是由示例库数据得出。
2. ``MemoryQualityEvaluator.evaluate(...)``：把当前观测特征与基准做**分位对比**，
   产出 ``MemoryRecallStrategy``（策略），质量分只是其中一条证据。

无基准 / 示例库为空时，调用方回退到 ``memory_analyzer`` 的启发式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .examples import RatingExample, RatingFeatures
from .strategy import MemoryRecallStrategy, utc_now

BASELINE_VERSION = "v1"


def _classify(v: RatingFeatures) -> str:
    """把一条特征归入“失败取向”：precision / coverage / balanced。"""
    if v.precision_failure >= v.coverage_failure:
        return "precision"
    return "coverage"


def _percentile(values: Sequence[float], p: float) -> float:
    sorted_values = sorted(float(v) for v in values)
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _separate_threshold(
    good: Sequence[float],
    poor: Sequence[float],
    *,
    default: float = 0.5,
) -> float:
    """在“标注好”与“标注差”之间求一条分离阈值。

    阈值由示例库数据决定（“基准”），不是写死的魔法数：取好样本的高位分位与差样本的
    低位分位的中间值；两类重叠时取均值中点；样本不全时回退 default。
    """
    # 空列表时 _percentile 返回 0.0，这里用 None 区分“确实没有此类”
    good_values = [float(v) for v in good]
    poor_values = [float(v) for v in poor]
    if good_values and poor_values:
        good_high = _percentile(good_values, 0.75)
        poor_low = _percentile(poor_values, 0.25)
        if poor_low > good_high:
            return (good_high + poor_low) / 2.0
        g_mean = sum(good_values) / len(good_values)
        p_mean = sum(poor_values) / len(poor_values)
        return (g_mean + p_mean) / 2.0
    if poor_values:
        return _percentile(poor_values, 0.25)
    if good_values:
        # 只有好样本：取“好样本高位再略上浮”，作为“比它更差才算异常”的参考。
        anchor = max(good_values)
        anchor = anchor if anchor > 0 else 0.25
        return _percentile(good_values + [anchor * 1.25], 0.75)
    return default


def _classify_values(examples: Sequence[RatingExample], mode: str) -> list[float]:
    return [
        e.features.precision_failure if mode == "precision" else e.features.coverage_failure
        for e in examples
        if _classify(e.features) == mode
    ]


@dataclass(frozen=True)
class BaselineStats:
    """从示例库校准出的参考基准。"""

    schema_version: str
    example_count: int
    good_count: int
    poor_count: int
    quality_percentiles: Mapping[str, float]  # p10..p90 的质量分
    precision_threshold: float  # “标注好”案例 precision_failure 的参考高位
    coverage_threshold: float  # “标注好”案例 coverage_failure 的参考高位
    precision_delta: int  # precision 差 → 对 level 的典型调整（通常 -1）
    coverage_delta: int  # coverage 差 → 对 level 的典型调整（通常 +1）
    calibrated_at: str = field(default_factory=lambda: utc_now().isoformat())

    def to_dict(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "example_count": self.example_count,
            "good_count": self.good_count,
            "poor_count": self.poor_count,
            "quality_percentiles": dict(self.quality_percentiles),
            "precision_threshold": self.precision_threshold,
            "coverage_threshold": self.coverage_threshold,
            "precision_delta": self.precision_delta,
            "coverage_delta": self.coverage_delta,
            "calibrated_at": self.calibrated_at,
        }


def calibrate(examples: Sequence[RatingExample]) -> BaselineStats | None:
    """用带标注示例求基准。没有可用的“好/差”样本时返回 None（回退启发式）。"""
    good = [e.features for e in examples if e.quality_label == "good"]
    poor = [e for e in examples if e.quality_label != "good"]

    good_scores = [f.quality_score for f in good]
    good_precision = [f.precision_failure for f in good]
    good_coverage = [f.coverage_failure for f in good]

    poor_precision = _classify_values(poor, "precision")
    poor_coverage = _classify_values(poor, "coverage")

    precision_threshold = _separate_threshold(good_precision, poor_precision)
    coverage_threshold = _separate_threshold(good_coverage, poor_coverage)
    percentiles = {
        key: _percentile(good_scores or [f.quality_score for f in poor] or [1.0], p)
        for key, p in (("p10", 0.10), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p90", 0.90))
    }

    precision_delta = _typical_delta(poor, "precision", default=-1)
    coverage_delta = _typical_delta(poor, "coverage", default=1)

    return BaselineStats(
        schema_version=BASELINE_VERSION,
        example_count=len(examples),
        good_count=len(good),
        poor_count=len(poor),
        quality_percentiles=percentiles,
        precision_threshold=round(precision_threshold, 4),
        coverage_threshold=round(coverage_threshold, 4),
        precision_delta=precision_delta,
        coverage_delta=coverage_delta,
    )


def _typical_delta(
    poor: Sequence[RatingExample], mode: str, *, default: int
) -> int:
    deltas = [
        e.truth_strategy.default_level - e.in_effect_level
        for e in poor
        if _classify(e.features) == mode or (e.truth_strategy.recall_mode == mode)
    ]
    if not deltas:
        return default
    # 取众数；平局取更保守（绝对值更小）的
    from collections import Counter

    counts = Counter(deltas)
    best = max(counts.values())
    candidates = [d for d, c in counts.items() if c == best]
    candidates.sort(key=abs)
    return candidates[0]


class MemoryQualityEvaluator:
    """用校准基准做分位对比，产出召回策略（质量分只是证据）。"""

    def __init__(self, baseline: BaselineStats) -> None:
        self.baseline = baseline

    @property
    def version(self) -> str:
        return f"baseline:{self.baseline.schema_version}"

    def evaluate(
        self, features: RatingFeatures, current_level: int
    ) -> MemoryRecallStrategy:
        b = self.baseline
        rel_precision = features.precision_failure - b.precision_threshold
        rel_coverage = features.coverage_failure - b.coverage_threshold

        evidence: list[Mapping[str, object]] = [
            {
                "feature": "quality_score",
                "value": features.quality_score,
                "reference_p50": b.quality_percentiles.get("p50", 0.5),
            },
            {
                "feature": "precision_failure",
                "value": features.precision_failure,
                "threshold": b.precision_threshold,
                "relative": rel_precision,
            },
            {
                "feature": "coverage_failure",
                "value": features.coverage_failure,
                "threshold": b.coverage_threshold,
                "relative": rel_coverage,
            },
        ]

        # 分位对比：哪条失败信号越过了它的参考阈值，就朝哪个取向给建议。
        # 两方向都越过时，取越过幅度更大者，避免同时升降。
        if rel_precision > 0 and rel_precision >= rel_coverage:
            mode, delta = "precision", b.precision_delta
        elif rel_coverage > 0:
            mode, delta = "coverage", b.coverage_delta
        else:
            mode, delta = "balanced", 0

        new_level = max(1, min(9, current_level + delta))
        if delta == 0:
            reason = "当前召回信号落在基准正常区间，档位保持不变。"
        elif mode == "precision":
            reason = "召回里“低相关/误导/冗余”占比超过基准高位，疑似召回太杂，降档以换取精度。"
        else:
            reason = "召回覆盖不足（thin/missing 或存在 gap），超过基准高位，升档以补缺口。"

        confidence = _confidence(rel_precision, rel_coverage, has_delta=delta != 0)

        return MemoryRecallStrategy(
            default_level=new_level,
            limit=None,  # 本期 limit 先不调；策略只动 level，后续可扩展
            recall_mode=mode,
            confidence=confidence,
            reason=reason,
            evidence=tuple(evidence),
            basis=self.version,
            updated_at=utc_now(),
        )


def _confidence(rel_precision: float, rel_coverage: float, *, has_delta: bool) -> float:
    if not has_delta:
        return 0.0
    margin = max(rel_precision, rel_coverage, 0.0)
    # 越界幅度映射到 0..1 置信度；0.5 起步、1.0 封顶，避免线性可解释性差。
    return round(max(0.5, min(1.0, 0.5 + margin)), 4)
