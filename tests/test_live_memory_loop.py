import importlib.util
from pathlib import Path

_LOOP = Path(__file__).resolve().parent / "live" / "memory_loop.py"
_spec = importlib.util.spec_from_file_location("memory_loop", _LOOP)
assert _spec and _spec.loader
memory_loop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(memory_loop)

analyze_recall = memory_loop.analyze_recall
analyze_seed = memory_loop.analyze_seed
parse_args = memory_loop.parse_args
_plan_from_args = memory_loop._plan_from_args


def test_plan_seed_and_recall_counts():
    args = parse_args(
        [
            "--goal",
            "提升回忆质量",
            "--seed",
            "3",
            "--recall",
            "3",
            "--speaker",
            "火星人",
        ]
    )
    plan = _plan_from_args(args)
    assert [item["phase"] for item in plan["turns"]] == [
        "seed",
        "seed",
        "seed",
        "recall",
        "recall",
        "recall",
    ]
    assert plan["flush_each"] is True
    assert "火卫一" in plan["turns"][0]["text"]


def test_plan_ingest_only():
    args = parse_args(["--scenario", "tests/live/scenarios/mars-ingest.json"])
    plan = _plan_from_args(args)
    assert len(plan["turns"]) == 3
    assert all(item["phase"] == "seed" for item in plan["turns"])
    assert plan["expect_seal"] == 1


def test_analyze_seed_and_recall_checks():
    seed_checks = analyze_seed(
        speaker="火星人",
        subject_name="匠石",
        markers=["火卫一"],
        expect_seal=1,
        inbound_objects={"火星人": "OBJ-M"},
        reply_kind="subject_reply",
        reply_text="记下了。（动作：点头）",
        delivery={"status": "ingested", "reason": "flush_max_segments"},
        new_events=[
            {
                "event_id": "EVT-1",
                "content_raw": "火星人：我的妹妹叫火卫一\n匠石：记下了。（动作：点头）",
                "peer_ids": ["OBJ-M"],
                "interlocutor": "OBJ-M",
            }
        ],
        unclosed_delta=0,
    )
    assert all(item["ok"] for item in seed_checks)
    recall_checks = analyze_recall(
        markers=["火卫一"],
        recalled_event_ids=["EVT-1"],
        memories=[{"content": "火星人提到火卫一"}],
        reply_text="你妹妹叫火卫一。",
        source_errors=[],
    )
    assert all(item["ok"] for item in recall_checks)
    empty = analyze_recall(
        markers=["火卫一"],
        recalled_event_ids=[],
        memories=[],
        reply_text="我想不起来。",
        source_errors=["memory:torch"],
    )
    assert empty[0]["ok"] is False
    assert any(not item["ok"] and item["name"] == "记起 火卫一" for item in empty)


def test_analyze_seed_ignores_unrelated_seal_if_fact_in_unclosed():
    checks = analyze_seed(
        speaker="火星人",
        subject_name="匠石",
        markers=["火卫二"],
        expect_seal=1,
        inbound_objects={"火星人": "OBJ-M"},
        reply_kind="subject_reply",
        reply_text="记下了。（动作：点头）",
        delivery={"status": "ingested", "reason": "flush_max_segments"},
        new_events=[
            {
                "event_id": "EVT-OLD",
                "content_raw": "哈喽，你会使用工具吗",
                "peer_ids": ["OBJ-M"],
                "interlocutor": "OBJ-M",
            }
        ],
        unclosed_delta=1,
        unclosed_after=[
            {
                "id": "UC-1",
                "content": "火星人：我的户籍在火卫二。\n匠石：记下了。（动作：点头）",
            }
        ],
    )
    by_name = {item["name"]: item for item in checks}
    assert by_name["本轮事实封存"]["ok"] is False
    assert by_name["事实已入 09"]["ok"] is True
    assert by_name["对话冠名"]["ok"] is True
    assert by_name["无无关封存"]["ok"] is False
    assert by_name["事实 火卫二"]["ok"] is True


def test_recall_question_echo_is_not_remembering():
    echoed = analyze_recall(
        markers=["尘埃帆"],
        recalled_event_ids=[],
        memories=[],
        reply_text="尘埃帆这个词我头一次听到。",
        source_errors=[],
        question="尘埃帆寄存在哪？帆角绣着什么？",
    )
    assert any(
        item["name"] == "口头独立答出 尘埃帆" and item["ok"] is False for item in echoed
    )
    assert any(item["name"] == "记起 尘埃帆" and item["ok"] is False for item in echoed)


def test_plan_explicit_turns_and_from_now():
    args = parse_args(
        [
            "--scenario",
            "tests/live/scenarios/mars-event-recall.json",
            "--max-turns",
            "2",
        ]
    )
    plan = _plan_from_args(args)
    assert [item["phase"] for item in plan["turns"]] == [
        "seed",
        "recall",
        "seed",
        "recall",
        "seed",
        "recall",
    ]
    assert plan["from_now"] is True
    assert plan["budget_cny"] == 5
    assert plan["turns"][0]["markers"] == ["火卫二", "赤-7719"]
    assert "火卫一" not in plan["turns"][0]["text"]


def test_flash_cost_offpeak():
    # 空闲：未命中 1.5、输出 4.5 / 百万
    assert abs(memory_loop.flash_cost_cny(1_000_000, 0, 0) - 1.5) < 1e-9
    assert abs(memory_loop.flash_cost_cny(0, 1_000_000, 0) - 4.5) < 1e-9
    assert abs(memory_loop.flash_cost_cny(1_000_000, 0, 1_000_000) - 0.05) < 1e-9


def test_parse_args_accepts_continue_and_one():
    args = parse_args(["--continue", ".pytest/live-loop/x", "--one"])
    assert args.continue_dir == ".pytest/live-loop/x"
    assert args.one is True


def test_forbid_test_store_rejects_pytest_dir():
    try:
        memory_loop._forbid_test_store(
            Path(__file__).resolve().parents[1] / ".pytest" / "mars-live"
        )
    except SystemExit as exc:
        assert ".jshi" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
