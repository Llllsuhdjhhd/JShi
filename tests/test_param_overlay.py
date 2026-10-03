"""阶段 0：覆盖层取代默认值，读取走当前快照，预检不写入。"""

from __future__ import annotations

import json

import pytest

from jshi.core.param_overlay import (
    OverlayError,
    load,
    override_snapshot,
    rollback,
    using_overlay,
    write_overlay,
)
from jshi.core.params import memory_flush_chars, suxipo_zone_chars
from jshi.effectiveness import MemoryRecallStrategy
from jshi.effectiveness.precheck import Change, Proposal, precheck
from jshi.effectiveness.suggestions import write_baseline_if_absent
from jshi.experienceledger import InProcessExperienceLedger
from jshi.memory import RecallStrategyStore
from jshi.style.packs import write_instruction_for, zone_chars_for


def test_overlay_replaces_default_and_does_not_add(tmp_path):
    path = tmp_path / "param_overlay.json"
    write_overlay(path, {"MEMORY_FLUSH_CHARS": 4000})
    with using_overlay(path):
        assert memory_flush_chars() == 4000
    assert memory_flush_chars() == 3000


def test_rollback_restores_previous_values(tmp_path, monkeypatch, capsys):
    path = tmp_path / "param_overlay.json"
    write_overlay(path, {"MEMORY_FLUSH_CHARS": 4000})
    write_overlay(path, {"MEMORY_FLUSH_CHARS": 5000, "WORKING_SET_LIMIT_CHARS": 2000})
    snapshot = rollback(path)
    assert snapshot.values == {"MEMORY_FLUSH_CHARS": 4000}
    assert "WORKING_SET_LIMIT_CHARS" not in snapshot.values
    assert snapshot.version == 3

    path.unlink()
    write_overlay(path, {"MEMORY_FLUSH_CHARS": 4000})
    write_overlay(path, {"MEMORY_FLUSH_CHARS": 5000, "WORKING_SET_LIMIT_CHARS": 2000})
    monkeypatch.setattr(
        "sys.argv",
        ["jshi", "--data-dir", str(tmp_path), "overlay", "--rollback"],
    )
    from jshi.app.cli import main

    main()
    assert capsys.readouterr().out.strip() == "3"
    loaded = load(path)
    assert loaded.values == {"MEMORY_FLUSH_CHARS": 4000}
    assert loaded.version == 3


def test_locked_unknown_and_recall_names_fail_load(tmp_path):
    path = tmp_path / "param_overlay.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "values": {"VALUE_NARRATION_CHARS": 400},
                "history": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(OverlayError) as locked:
        load(path)
    assert str(path) in str(locked.value)
    assert "VALUE_NARRATION_CHARS" in str(locked.value)

    path.write_text(
        json.dumps({"version": 1, "values": {"NOT_A_PARAM": 1}, "history": []}),
        encoding="utf-8",
    )
    with pytest.raises(OverlayError) as unknown:
        load(path)
    assert "NOT_A_PARAM" in str(unknown.value)

    path.write_text(
        json.dumps(
            {"version": 1, "values": {"recall.default_level": 3}, "history": []}
        ),
        encoding="utf-8",
    )
    with pytest.raises(OverlayError) as recall:
        load(path)
    assert "recall.default_level" in str(recall.value)


def test_out_of_range_rejected(tmp_path):
    path = tmp_path / "param_overlay.json"
    with pytest.raises(OverlayError) as exc:
        write_overlay(path, {"MEMORY_FLUSH_CHARS": 9000})
    assert "MEMORY_FLUSH_CHARS" in str(exc.value)
    assert not path.exists()


def test_suxipo_zone_chars_follow_snapshot():
    assert zone_chars_for("suxipo") == 5000
    with override_snapshot({"SUXIPO_ZONE_CHARS": 8000}):
        assert zone_chars_for("suxipo") == 8000
        assert suxipo_zone_chars() == 8000
        text = write_instruction_for("suxipo", zone_chars=zone_chars_for("suxipo"))
        assert "8000" in text
    assert zone_chars_for("suxipo") == 5000


def test_ledger_reads_snapshot_not_import_time_constant():
    with override_snapshot({"ACTIVE_ZONE_RATIO": 0.1}):
        ledger = InProcessExperienceLedger()
        assert ledger.active_window_chars == 100_000
    assert InProcessExperienceLedger().active_window_chars == 33_333


def test_constraint_companion_is_the_minimum():
    current = {"MEMORY_FLUSH_CHARS": 3000, "SUXIPO_ZONE_CHARS": 5000}
    result = precheck(
        Proposal(
            counter_id="ratings.missing_coverage",
            sample_activity_ids=("act-1",),
            cause="投递阈值升高后片场装不下未投递的原话",
            window_start="2026-09-30T00:00:00+00:00",
            window_end="2026-09-30T01:00:00+00:00",
            proposed=Change(
                name="MEMORY_FLUSH_CHARS", old=3000, new=4000, store="overlay"
            ),
            current=current,
        )
    )
    assert result.companion is not None
    assert result.companion.name == "SUXIPO_ZONE_CHARS"
    assert result.companion.new == 6000
    assert result.ok is False
    assert "review" in result.reason

    wrong = precheck(
        Proposal(
            counter_id="ratings.missing_coverage",
            sample_activity_ids=("act-1",),
            cause="绑定项写多了",
            window_start="2026-09-30T00:00:00+00:00",
            window_end="2026-09-30T01:00:00+00:00",
            proposed=Change(
                name="MEMORY_FLUSH_CHARS", old=3000, new=4000, store="overlay"
            ),
            companion=Change(
                name="SUXIPO_ZONE_CHARS", old=5000, new=9000, store="overlay"
            ),
            current=current,
        )
    )
    assert wrong.ok is False
    assert "约束" in wrong.reason


def test_install_twice_is_refused(tmp_path):
    path = tmp_path / "param_overlay.json"
    write_overlay(path, {"MEMORY_FLUSH_CHARS": 4000})
    from jshi.core.param_overlay import install

    with using_overlay(path):
        with pytest.raises(OverlayError):
            install(path)


def test_baseline_records_levels_once(tmp_path):
    path = tmp_path / "tuning_suggestions.jsonl"
    strategy = RecallStrategyStore(tmp_path / "recall_strategy.json")
    strategy.apply("stone", default_level=2, limit=6)
    assert write_baseline_if_absent(path, strategy, ("stone", "other"))
    assert write_baseline_if_absent(path, strategy, ("stone",)) is False
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["status"] == "baseline"
    assert rows[0]["subjects"]["stone"] == {"default_level": 2, "limit": 6}
    assert rows[0]["subjects"]["other"] == {"default_level": 1, "limit": None}


def test_missing_overlay_file_does_not_create(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["jshi", "--data-dir", str(tmp_path), "overlay"],
    )
    from jshi.app.cli import main

    with pytest.raises(SystemExit) as exc:
        main()
    assert "param_overlay.json" in str(exc.value)
    assert not (tmp_path / "param_overlay.json").exists()


def test_both_recall_changes_keep_only_level(tmp_path):
    from jshi.effectiveness import InProcessEffectiveness
    from jshi.models import MemoryRating, MemoryRatings

    class Both:
        def evaluate(self, features, current_level):
            del features
            return MemoryRecallStrategy(
                default_level=current_level - 1,
                limit=4,
                reason="档位和条数一起变",
            )

    strategy = RecallStrategyStore(tmp_path / "recall_strategy.json")
    strategy.apply("stone", default_level=2, limit=1)
    eff = InProcessEffectiveness(
        strategy=strategy,
        suggestions_path=tmp_path / "tuning_suggestions.jsonl",
        evaluator=Both(),
    )
    ratings = MemoryRatings(
        items=(MemoryRating(ref="memory:x", relevance="unrelated"),),
        coverage="sufficient",
    )
    for index in range(8):
        eff.record_ratings("stone", f"act-{index}", ratings)
    eff.run_due("stone")
    assert strategy.get("stone").default_level == 2
    assert strategy.get("stone").limit == 1
    proposed = eff.suggestions.rows[-1]["proposed"]
    assert proposed["name"] == "recall.default_level"
    assert eff.suggestions.rows[-1]["companion"] is None


def test_counter_is_stable_for_the_same_window():
    from datetime import datetime, timezone

    from jshi.effectiveness.counters import CountWindow, ratings_unrelated_ratio
    from jshi.effectiveness.ratings import RatingRow

    start = datetime(2026, 9, 30, tzinfo=timezone.utc)
    rows = [
        RatingRow(
            subject_id="stone",
            activity_id="a1",
            items=[{"relevance": "unrelated"}],
            created_at=start,
        ),
        RatingRow(
            subject_id="stone",
            activity_id="a2",
            items=[{"relevance": "related"}],
            created_at=start,
        ),
    ]
    window = CountWindow(start=start, end=start)
    first = ratings_unrelated_ratio(rows, window)
    second = ratings_unrelated_ratio(rows, window)
    assert first == second
    assert first.count == 0.5
    assert first.activity_ids == ("a1",)


def test_step_inputs_dedupe_system_prompt_and_record_both_purposes(tmp_path):
    from jshi.effectiveness.step_inputs import StepInputStore
    from tests.test_effectiveness import FixedModel, runtime

    store = StepInputStore(tmp_path / "manual.jsonl")
    store.append_call(
        subject_id="stone",
        activity_id="a",
        purpose="subject_activity",
        model="m",
        system_text="同一份系统提示词",
        user_text="问",
    )
    store.append_call(
        subject_id="stone",
        activity_id="a",
        purpose="write_zone",
        model="m",
        system_text="同一份系统提示词",
        user_text="写",
    )
    manual = [
        json.loads(line)
        for line in (tmp_path / "manual.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert sum(1 for row in manual if row["kind"] == "prompt") == 1

    process, _repository = runtime(tmp_path)
    process.write_zone = FixedModel()
    process.experience("stone", "你好", object_ref="user")
    recorded = [
        json.loads(line)
        for line in (tmp_path / "step_inputs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    calls = [row for row in recorded if row.get("kind") == "call"]
    prompts = [row for row in recorded if row.get("kind") == "prompt"]
    assert {row["purpose"] for row in calls} >= {"subject_activity", "write_zone"}
    assert len({row["hash"] for row in prompts}) == len(prompts)


def test_rejected_suggestion_does_not_change_level(tmp_path):
    from jshi.effectiveness import InProcessEffectiveness
    from jshi.models import MemoryRating, MemoryRatings

    class NoCause:
        def evaluate(self, features, current_level):
            del features
            return MemoryRecallStrategy(default_level=current_level - 1, reason="  ")

    strategy = RecallStrategyStore(tmp_path / "recall_strategy.json")
    strategy.apply("stone", default_level=2)
    eff = InProcessEffectiveness(
        strategy=strategy,
        suggestions_path=tmp_path / "tuning_suggestions.jsonl",
        evaluator=NoCause(),
    )
    ratings = MemoryRatings(
        items=(MemoryRating(ref="memory:x", relevance="unrelated"),),
        coverage="sufficient",
    )
    for index in range(8):
        eff.record_ratings("stone", f"act-{index}", ratings)
    eff.run_due("stone")
    assert strategy.get("stone").default_level == 2
    assert eff.suggestions.rows[-1]["status"] == "rejected"
    assert eff.suggestions.rows[-1]["applied"] is False
