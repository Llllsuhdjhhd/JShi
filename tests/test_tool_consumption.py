"""【工具相关】的话题匹配、消化标记、记挂退出与账本压缩。"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from jshi.tool import HangStore, StubEngine, ToolModule, ToolRunner, ToolService
from jshi.tool.contract import utc_now
from jshi.tool.service import RELEVANT_CAP, topic_terms


def _service(tmp_path: Path, store: HangStore | None = None) -> tuple[ToolService, HangStore]:
    hang = store or HangStore(tmp_path / "hang.jsonl")
    service = ToolService(
        hang,
        ToolRunner(ToolModule(StubEngine()), hang),
        intake_path=tmp_path / "tool.jsonl",
    )
    return service, hang


def _done(hang: HangStore, need: str, summary: str, *, consumed: bool = True):
    record = hang.create(subject_id="stone", object_id="OBJ-A", need=need, command="search")
    hang.set_wrap(record.task_id, visible=True, summary=summary, terminal=True)
    if consumed:
        hang.set_delivered(record.task_id)
    return record


def test_topic_terms_drop_function_words():
    assert topic_terms("可以再说一下吗") == frozenset()
    assert topic_terms("帮我查一下明天的天气") == {"明天", "天气"}


def test_consumed_result_needs_two_topic_terms(tmp_path):
    service, hang = _service(tmp_path)
    weather = _done(hang, "查杭州明天天气", "杭州明天有雨")
    assert service.list_tool_related_entries("stone", "OBJ-A", query="可以再说一下吗") == ()
    assert service.list_tool_related_entries("stone", "OBJ-A", query="今天天气不错") == ()
    again = service.list_tool_related_entries("stone", "OBJ-A", query="明天天气会下雨吗")
    assert [item["id"] for item in again] == [weather.task_id]


def test_relevant_old_results_are_capped(tmp_path):
    service, hang = _service(tmp_path)
    for index in range(6):
        _done(hang, f"查杭州天气第{index}次", f"结果{index}")
    picked = service.list_tool_related_entries("stone", "OBJ-A", query="杭州天气怎样")
    assert len(picked) == RELEVANT_CAP


def test_unconsumed_result_repeats_until_consumed_then_expires(tmp_path):
    service, hang = _service(tmp_path)
    full = "甄士隐梦幻识通灵" * 1200
    record = _done(hang, "取红楼梦第一回全文", full, consumed=False)
    for _ in range(3):
        shown = service.list_tool_related_entries("stone", "OBJ-A", query="聊别的")
        assert [item["id"] for item in shown] == [record.task_id]
        assert shown[0]["final_result"] == full
    later = utc_now() + timedelta(hours=25)
    assert service.list_tool_related_entries("stone", "OBJ-A", query="聊别的", now=later) == ()


def test_write_back_does_not_keep_open_hang_alive(tmp_path):
    service, hang = _service(tmp_path)
    record = hang.create(subject_id="stone", object_id="OBJ-A", need="长任务", command="search")
    stamp = utc_now()
    hang._records[record.task_id] = replace(
        hang.get(record.task_id), updated_at=stamp - timedelta(hours=2)
    )
    before = hang.get(record.task_id).updated_at
    service.write_back_response((record.task_id,), {"mode": "respond", "reply": "还在办"})
    hang.set_delivered(record.task_id)
    assert hang.get(record.task_id).updated_at == before
    service.reap_stale("stone", "OBJ-A", now=stamp)
    assert hang.get(record.task_id).status == "cancelled"


def test_open_hang_has_total_lifetime_cap(tmp_path):
    service, hang = _service(tmp_path)
    record = hang.create(subject_id="stone", object_id="OBJ-A", need="一直有进度", command="search")
    stamp = utc_now()
    hang._records[record.task_id] = replace(
        hang.get(record.task_id),
        created_at=stamp - timedelta(hours=25),
        updated_at=stamp - timedelta(minutes=1),
    )
    service.reap_stale("stone", "OBJ-A", now=stamp)
    assert hang.get(record.task_id).status == "cancelled"


def test_hang_file_is_compacted_and_responses_are_capped(tmp_path):
    path = tmp_path / "hang.jsonl"
    store = HangStore(path)
    record = store.create(subject_id="stone", object_id="OBJ-A", need="查", command="search")
    for index in range(200):
        store.set_response(record.task_id, {"mode": "respond", "reply": f"第{index}轮"})
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) < 100
    loaded = HangStore(path)
    got = loaded.get(record.task_id)
    assert got is not None
    assert len(got.responses) == 20
    assert got.responses[-1]["reply"] == "第199轮"


def test_hang_file_cap_drops_oldest_ended_records_only(tmp_path):
    path = tmp_path / "hang.jsonl"
    store = HangStore(path, max_bytes=20_000)
    running = store.create(subject_id="stone", object_id="OBJ-A", need="在办", command="search")
    ended = []
    for index in range(12):
        record = store.create(subject_id="stone", object_id="OBJ-A", need=f"旧{index}", command="search")
        store.set_wrap(record.task_id, visible=True, summary="结果" * 1000, terminal=True)
        ended.append(record.task_id)
    assert path.stat().st_size <= 20_000
    assert store.get(running.task_id) is not None
    assert store.get(ended[-1]) is not None
    assert store.get(ended[0]) is None
    reloaded = HangStore(path, max_bytes=20_000)
    assert reloaded.get(running.task_id) is not None
