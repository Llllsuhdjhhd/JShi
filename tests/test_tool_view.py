"""工具过程只读视图。"""

from __future__ import annotations

from pathlib import Path

from jshi.tool import HangStore, StubEngine, ToolModule, ToolRunner, ToolService
from jshi.tool.view import format_tool_now, format_tool_plan_view, format_tool_process


def test_format_tool_process_shows_05_intake_hang_and_wrap(tmp_path: Path) -> None:
    store = HangStore(tmp_path / "hang.jsonl")
    runner = ToolRunner(ToolModule(StubEngine()), store)
    service = ToolService(
        store,
        runner,
        intake_path=tmp_path / "tool.jsonl",
    )
    service.intake(
        subject_id="stone",
        object_id="OBJ-A",
        need="查一下天气",
        verbal="我去查一下",
    )
    service.drain_for_tests()
    text = format_tool_process(
        service,
        subject_id="stone",
        object_id="OBJ-A",
        last_use_tool=True,
        last_need="查一下天气",
        last_verbal="我去查一下",
    )
    assert "【05 触发】" in text
    assert "use_tool=true" in text
    assert "【200 交接】" in text
    assert "查一下天气" in text
    assert "我去查一下" in text
    assert "launched" in text
    assert "【记挂 210】" in text
    assert "【200 对记挂的处理（包装）】" in text
    assert "【引擎反馈】" in text
    assert "estimate" in text
    assert "progress" in text
    assert "result" in text
    assert "【交付片场】" in text
    assert "【05 回写】" in text
    assert "【当前可见（未交付）】" in text
    listing = format_tool_process(
        service,
        subject_id="stone",
        object_id="OBJ-A",
        listing=True,
    )
    assert "【交接一览】" in listing
    assert "【记挂一览】" in listing
    now_text = format_tool_now(
        service,
        subject_id="stone",
        object_id="OBJ-A",
    )
    assert "【当前工具】" in now_text
    assert "# 使用中" in now_text
    assert "# 刚使用" in now_text
    assert "查一下天气" in now_text
    assert "描述：" in now_text
    assert "最近反馈：" in now_text
    missing = format_tool_process(
        service,
        subject_id="stone",
        object_id="OBJ-A",
        item_id="no-such-id",
    )
    assert "找不到" in missing


def test_format_tool_process_matches_short_id(tmp_path: Path) -> None:
    store = HangStore(tmp_path / "hang.jsonl")
    runner = ToolRunner(ToolModule(StubEngine()), store)
    service = ToolService(
        store,
        runner,
        intake_path=tmp_path / "tool.jsonl",
    )
    service.intake(
        subject_id="stone",
        object_id="OBJ-A",
        need="查一下天气",
        verbal="我去查一下",
    )
    service.drain_for_tests()
    record = service.intake_store.list_for("stone", "OBJ-A")[0]
    by_intake = format_tool_process(
        service,
        subject_id="stone",
        object_id="OBJ-A",
        item_id=record.intake_id[:8],
    )
    assert record.intake_id in by_intake
    assert "找不到" not in by_intake
    assert "不唯一" not in by_intake
    by_task = format_tool_process(
        service,
        subject_id="stone",
        object_id="OBJ-A",
        item_id=record.task_id[:8],
    )
    assert record.task_id in by_task
    too_short = format_tool_process(
        service,
        subject_id="stone",
        object_id="OBJ-A",
        item_id="ins",
    )
    assert "找不到" in too_short


def test_format_tool_process_short_id_ambiguous(tmp_path: Path) -> None:
    store = HangStore(tmp_path / "hang.jsonl")
    runner = ToolRunner(ToolModule(StubEngine()), store)
    service = ToolService(
        store,
        runner,
        intake_path=tmp_path / "tool.jsonl",
    )
    service.intake_store.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="甲",
        intake_id="aaaa1111-0000-4000-8000-000000000001",
    )
    service.intake_store.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="乙",
        intake_id="aaaa2222-0000-4000-8000-000000000002",
    )
    text = format_tool_process(
        service,
        subject_id="stone",
        object_id="OBJ-A",
        item_id="aaaa",
    )
    assert "不唯一" in text
    assert "aaaa1111" in text
    assert "aaaa2222" in text


def test_format_tool_plan_view_shows_live_scene(tmp_path: Path) -> None:
    store = HangStore(tmp_path / "hang.jsonl")
    runner = ToolRunner(ToolModule(StubEngine()), store)
    service = ToolService(
        store,
        runner,
        intake_path=tmp_path / "tool.jsonl",
    )
    service.intake(
        subject_id="stone",
        object_id="OBJ-A",
        need="查一下天气",
        verbal="我去查一下",
        field_ref={"zone_kind": "wood", "zone_rev": "1"},
    )
    service.drain_for_tests()
    marker = "MARKER-PLAN-SCENE"
    text = format_tool_plan_view(
        service,
        subject_id="stone",
        object_id="OBJ-A",
        scene_loader=lambda _intake: marker,
    )
    assert "【205 策划材料】" in text
    assert "【scene · 现读】" in text
    assert marker in text
    assert "查一下天气" in text
    assert "【catalog】" in text
    assert "echo" in text
    missing = format_tool_plan_view(
        service,
        subject_id="stone",
        object_id="OBJ-A",
        item_id="no-such-id",
        scene_loader=lambda _intake: marker,
    )
    assert "找不到" in missing
