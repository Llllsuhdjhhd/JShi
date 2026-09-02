"""有效性分析基准框架：示例库校准 + 分位对比给出“策略”（不是分数）。"""

from __future__ import annotations

from jshi.effectiveness import (
    JsonlRatingStore,
    MemoryQualityEvaluator,
    MemoryRecallStrategy,
    RatingExample,
    RatingFeatures,
    calibrate,
    features_from_ratings,
    read_examples,
    write_examples,
)
from jshi.memory import RecallStrategyStore
from jshi.models import MemoryRating, MemoryRatings


def _example(
    features: RatingFeatures,
    label: str,
    *,
    in_level: int,
    delta: int,
    mode: str,
    reason: str = "",
) -> RatingExample:
    return RatingExample(
        subject_id="stone",
        quality_label=label,
        in_effect_level=in_level,
        features=features,
        truth_strategy=MemoryRecallStrategy(
            default_level=in_level + delta, recall_mode=mode
        ),
        reason=reason,
    )


def _good_features() -> RatingFeatures:
    return features_from_ratings(
        MemoryRatings(
            items=(MemoryRating(ref="m:a", relevance="related", helps_understanding=2),),
            coverage="sufficient",
        )
    )


def _precision_poor_features() -> RatingFeatures:
    return features_from_ratings(
        MemoryRatings(
            items=(MemoryRating(ref="m:a", relevance="unrelated"),),
            coverage="sufficient",
        )
    )


def _coverage_poor_features() -> RatingFeatures:
    return features_from_ratings(
        MemoryRatings(coverage="missing", gap_query="lux 岭南")
    )


def test_features_from_ratings_and_rows_consistent():
    ratings = MemoryRatings(
        items=(
            MemoryRating(ref="m:a", relevance="related", helps_understanding=2),
            MemoryRating(ref="m:b", relevance="unrelated"),
        ),
        coverage="thin",
        gap_query="mei",
    )
    single = features_from_ratings(ratings)
    assert single.rated_count == 2
    assert single.unrelated_ratio == 0.5
    assert single.has_gap is True
    assert single.coverage == "thin"
    # 分数只是证据：它由失败信号导出，而非独立“结论”。
    assert 0.0 <= single.quality_score <= 1.0
    assert single.precision_failure >= 0.0
    assert single.coverage_failure >= 0.6


def test_calibrate_produces_reference_from_examples():
    examples = [
        _example(_good_features(), "good", in_level=3, delta=0, mode="balanced"),
        _example(_good_features(), "good", in_level=3, delta=0, mode="balanced"),
        _example(_precision_poor_features(), "poor", in_level=3, delta=-1, mode="precision"),
        _example(_coverage_poor_features(), "poor", in_level=3, delta=1, mode="coverage"),
    ]
    baseline = calibrate(examples)
    assert baseline is not None
    assert baseline.example_count == 4
    assert baseline.good_count == 2
    assert baseline.poor_count == 2
    assert baseline.precision_delta == -1
    assert baseline.coverage_delta == 1
    assert "p50" in baseline.quality_percentiles


def test_evaluator_outputs_strategy_not_score():
    good = _good_features()
    precision_poor = _precision_poor_features()
    coverage_poor = _coverage_poor_features()
    baseline = calibrate(
        [
            _example(good, "good", in_level=3, delta=0, mode="balanced"),
            _example(good, "good", in_level=3, delta=0, mode="balanced"),
            _example(precision_poor, "poor", in_level=3, delta=-1, mode="precision"),
            _example(coverage_poor, "poor", in_level=3, delta=1, mode="coverage"),
        ]
    )
    evaluator = MemoryQualityEvaluator(baseline)

    # 输出是“策略”，不是分数：是一个带召回参数的 MemoryRecallStrategy。
    for obs in (precision_poor, coverage_poor):
        strategy = evaluator.evaluate(obs, current_level=3)
        assert isinstance(strategy, MemoryRecallStrategy)
        assert 1 <= strategy.default_level <= 9
        assert strategy.recall_mode in {"precision", "coverage", "balanced"}
        assert strategy.basis.startswith("baseline:")
        # 质量分只作为证据出现，不对应策略顶层字段。
        assert not hasattr(strategy, "quality_score")
        labels = {str(e.get("feature")) for e in strategy.evidence}
        assert "quality_score" in labels

    # precision 失败 → 降档；coverage 失败 → 升档。
    down = evaluator.evaluate(precision_poor, current_level=3)
    assert down.recall_mode == "precision"
    assert down.default_level < 3
    assert down.confidence > 0.0

    up = evaluator.evaluate(coverage_poor, current_level=3)
    assert up.recall_mode == "coverage"
    assert up.default_level > 3

    # 基准正常区间 → 保持不变。
    hold = evaluator.evaluate(good, current_level=5)
    assert hold.recall_mode == "balanced"
    assert hold.default_level == 5
    assert hold.confidence == 0.0


def test_examples_jsonl_roundtrip(tmp_path):
    path = tmp_path / "examples.jsonl"
    good = _good_features()
    poor = _coverage_poor_features()
    examples = [
        _example(good, "good", in_level=3, delta=0, mode="balanced", reason="够用"),
        _example(poor, "poor", in_level=4, delta=1, mode="coverage", reason="缺 mei"),
    ]
    write_examples(path, examples)
    loaded = read_examples(path)
    assert len(loaded) == 2
    assert loaded[0].quality_label == "good"
    assert loaded[0].features.rated_count == 1
    assert loaded[1].truth_strategy.default_level == 5
    assert loaded[1].truth_strategy.recall_mode == "coverage"


def test_inprocess_uses_baseline_when_available(tmp_path):
    from jshi.effectiveness import InProcessEffectiveness

    examples_path = tmp_path / "examples.jsonl"
    write_examples(
        examples_path,
        [
            _example(_good_features(), "good", in_level=1, delta=0, mode="balanced"),
            _example(
                coverage_poor := _coverage_poor_features(),
                "poor",
                in_level=1,
                delta=1,
                mode="coverage",
            ),
        ],
    )
    strategy = RecallStrategyStore(tmp_path / "recall_strategy.json")
    eff = InProcessEffectiveness(
        strategy=strategy, examples_path=examples_path
    )
    # 造足够多的 coverage 缺失打分，触发 run_due。
    rows = memory_ratings_missing_8()
    for index, ratings in enumerate(rows):
        eff.record_ratings("stone", f"act-{index}", ratings)

    eff.run_due("stone")
    assert strategy.get("stone").default_level > 1
    assert strategy.get("stone").source_report_id
    assert any(
        report.strategy.get("basis", "").startswith("baseline:")
        for report in eff.reports("stone")
    )


def memory_ratings_missing_8():
    ratings = MemoryRatings(coverage="missing", gap_query="lux 岭南")
    return [ratings for _ in range(8)]


def test_no_baseline_falls_back_to_heuristic(tmp_path):
    from jshi.effectiveness import InProcessEffectiveness
    from jshi.effectiveness.memory_analyzer import heuristic_strategy

    strategy = RecallStrategyStore(tmp_path / "recall_strategy.json")
    strategy.apply("stone", default_level=2)
    eff = InProcessEffectiveness(strategy=strategy)
    ratings = MemoryRatings(
        items=(MemoryRating(ref="memory:x", relevance="unrelated"),),
        coverage="sufficient",
    )
    for index in range(8):
        eff.record_ratings("stone", f"act-{index}", ratings)
    eff.run_due("stone")
    assert strategy.get("stone").default_level == 1
    assert strategy.get("stone").source_report_id


def test_inprocess_collects_unlabeled_candidates(tmp_path):
    from jshi.effectiveness import InProcessEffectiveness

    candidate_path = tmp_path / "candidates.jsonl"
    eff = InProcessEffectiveness(candidate_path=candidate_path)
    ratings = MemoryRatings(
        items=(MemoryRating(ref="memory:x", relevance="unrelated"),),
        coverage="missing",
        gap_query="mei",
    )
    eff.record_ratings("stone", "act-1", ratings)
    text = candidate_path.read_text(encoding="utf-8")
    assert '"quality_label": "unlabeled"' in text
    assert '"has_gap": true' in text
    assert '"coverage": "missing"' in text
