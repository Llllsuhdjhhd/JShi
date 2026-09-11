"""工具过程只读视图。"""

from __future__ import annotations

from pathlib import Path

from jshi.tool import HangStore, StubEngine, ToolModule, ToolRunner, ToolService
from jshi.tool.view import format_tool_process


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
    assert "【03 将装】" in text
    listing = format_tool_process(
        service,
        subject_id="stone",
        object_id="OBJ-A",
        listing=True,
    )
    assert "【交接一览】" in listing
    assert "【记挂一览】" in listing
    missing = format_tool_process(
        service,
        subject_id="stone",
        object_id="OBJ-A",
        item_id="no-such-id",
    )
    assert "找不到" in missing
