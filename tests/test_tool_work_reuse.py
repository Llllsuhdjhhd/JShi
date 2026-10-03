from dataclasses import replace
from datetime import timedelta
import json

import pytest

from jshi.models import ModelResponse
from jshi.skill.cognition import _persona_to_model_response, _to_model_response
from jshi.tool import (AskMode, FeedbackKind, HangStore, ToolFeedback, ToolModule,
                       ToolRequest, ToolResult, ToolRunner, ToolService, ToolStatus)
from jshi.tool.contract import utc_now
from jshi.tool.plan import ToolPlan, ToolStep
from jshi.tool.reuse import ReusePolicy
from jshi.tool.pi_engine import _result_payload


class Engine:
    name = "counting"

    def __init__(self):
        self.calls = []

    def list_templates(self):
        return ("weather",)

    def execute(self, request):
        self.calls.append(request)
        return (ToolFeedback(request_id=request.request_id, kind=FeedbackKind.RESULT,
            result=ToolResult(status=ToolStatus.OK, ideal=True,
                result={"coverage": dict(request.params), "content": "weather data"},
                summary="weather data")),)


def runtime(path, policies=None):
    store = HangStore(path / "hang.jsonl")
    engine = Engine()
    service = ToolService(store, ToolRunner(ToolModule(engine), store), reuse_policies=policies)
    return service, engine


def launch(service, request, need="weather", refresh=""):
    record = service.intake_store.create(subject_id="s", object_id="o", need=need)
    if refresh:
        record = service.intake_store.update(record.intake_id, meta={"refresh_reason": refresh})
    task_id = service._launch(record, request)
    service.drain_for_tests()
    return task_id


def handle(service, ids, disposition="deferred", reply="", complete=False, unsaid=""):
    return service.handle_results("s", "o", [
        {"task_id": task_id, "disposition": disposition, "evidence": reply,
         "reason": "pending itinerary", "work_complete": complete} for task_id in ids
    ], selected_ids=ids, reply=reply, unsaid=unsaid)


def test_deferred_survives_topic_change_age_and_restart(tmp_path):
    service, _ = runtime(tmp_path)
    task_id = launch(service, ToolRequest(command="weather", params={"city": "x"}))
    handle(service, [task_id])
    later = utc_now() + timedelta(days=3)
    service2, engine2 = runtime(tmp_path)
    entries = service2.list_tool_related_entries("s", "o", query="continue the plan", now=later)
    assert [e["id"] for e in entries] == [task_id]
    assert entries[0]["handling"] == "deferred"
    assert not engine2.calls
    assert service2.list_tool_related_entries("s", "someone_else", query="continue") == ()


def test_fake_delivery_or_unsaid_cannot_close_work(tmp_path):
    service, _ = runtime(tmp_path)
    task_id = launch(service, ToolRequest(command="weather"))
    accepted = service.handle_results("s", "o", [
        {"task_id": task_id, "disposition": "answered", "evidence": "made up", "work_complete": True}
    ], selected_ids=[task_id], reply="wait a moment")
    assert accepted == ()
    assert not service.work_index.for_task(task_id)[0]["closed"]
    handle(service, [task_id], "answered", "weather data", True, unsaid="itinerary pending")
    assert not service.work_index.for_task(task_id)[0]["closed"]
    handle(service, [task_id], "answered", "weather data", True)
    assert service.work_index.for_task(task_id)[0]["closed"]
    service.mark_main_seen("s", "o", item_ids=[task_id])
    assert service.list_tool_related_entries("s", "o", query="unrelated") == ()


def test_plan_relationship_restored_without_execution(tmp_path):
    service, _ = runtime(tmp_path)
    intake = service.intake_store.create(subject_id="s", object_id="o", need="two day itinerary")
    plan = ToolPlan("trip", "two day itinerary", "parallel", (
        ToolStep("weather", ToolRequest(command="weather")),
        ToolStep("train", ToolRequest(command="train")),
        ToolStep("ticket", ToolRequest(command="ticket")),
    ))
    service._launch_plan(intake, plan)
    service.drain_for_tests()
    ids = list(service.work_index.get("trip")["tasks"].values())
    handle(service, ids, "answered", "data returned")  # step use does not finish whole work
    service.mark_main_seen("s", "o", item_ids=ids)
    restarted, engine = runtime(tmp_path)
    assert restarted.plan_summary("trip")["status"] == "done"
    assert {e["id"] for e in restarted.list_tool_related_entries("s", "o", query="continue")} == set(ids)
    assert not engine.calls
    handle(restarted, ids, "answered", "complete itinerary delivered", True)
    assert restarted.work_index.get("trip")["closed"]


def test_incomplete_plan_cannot_be_closed_and_pending_steps_displayed(tmp_path):
    service, _ = runtime(tmp_path)
    task_id = launch(service, ToolRequest(command="weather"))
    plan = {"plan_id": "trip", "mode": "sequential", "steps": [
        {"step_id": "weather"}, {"step_id": "ticket"}]}
    service.work_index.create("trip", "s", "o", "itinerary", plan)
    service.work_index.bind("trip", task_id, "weather")
    handle(service, [task_id], "answered", "weather done", True)
    assert not service.work_index.get("trip")["closed"]
    entry = service.list_tool_related_entries("s", "o", query="continue")[0]
    assert entry["pending_steps"] == ["ticket"]


def test_reuse_runs_no_engine_and_preserves_original_query_time(tmp_path):
    policy = ReusePolicy(600, ("city", "start", "end"), ("start", "end"))
    service, engine = runtime(tmp_path, {"weather": policy})
    request = ToolRequest(command="weather", params={"city": "Ｘ", "start": "2026-10-04", "end": "2026-10-07"})
    first = launch(service, request)
    original_at = service.hang_store.get(first).feedback[-1].at
    second = launch(service, replace(request, params={"city": " X ", "start": "2026-10-05", "end": "2026-10-06"}), need="different wording")
    assert len(engine.calls) == 1
    assert first != second
    record = service.hang_store.get(second)
    assert record.meta["reused_from"] == first
    assert record.feedback[-1].at == original_at
    assert record.feedback[-1].result.time_ms == 0
    third = launch(service, request, refresh="user requested update")
    assert len(engine.calls) == 2
    assert "reused_from" not in service.hang_store.get(third).meta


@pytest.mark.parametrize("failure", ["failed", "partial", "missing_coverage", "short_coverage", "expired", "invalidated", "not_ideal", "wrong_city"])
def test_reuse_rejects_unsuitable_results(tmp_path, failure):
    policy = ReusePolicy(600, ("city", "start", "end"), ("start", "end"))
    service, engine = runtime(tmp_path, {"weather": policy})
    request = ToolRequest(command="weather", params={"city": "x", "start": "2026-10-04", "end": "2026-10-07"})
    first = launch(service, request)
    record = service.hang_store.get(first)
    feedback = record.feedback[-1]
    result = feedback.result
    if failure in {"failed", "partial"}:
        result = replace(result, status=ToolStatus(failure))
    elif failure == "missing_coverage":
        result = replace(result, result={"content": "data"})
    elif failure == "short_coverage":
        result = replace(result, result={"coverage": {**request.params, "end": "2026-10-04"}})
    elif failure == "not_ideal":
        result = replace(result, ideal=False)
    elif failure == "wrong_city":
        request = replace(request, params={**request.params, "city": "y"})
    elif failure == "invalidated":
        service.invalidate(first, reason="incorrect")
    at = utc_now() - timedelta(hours=1) if failure == "expired" else feedback.at
    service.hang_store._records[first] = replace(
        service.hang_store.get(first),
        feedback=(*record.feedback[:-1], replace(feedback, result=result, at=at)),
    )
    launch(service, request)
    assert len(engine.calls) == 2


def test_unknown_and_proposal_tools_do_not_reuse(tmp_path):
    service, engine = runtime(tmp_path)
    request = ToolRequest(command="weather", params={"city": "x"})
    launch(service, request)
    launch(service, request)
    assert len(engine.calls) == 2
    service.reuse_policies["weather"] = ReusePolicy(600, ("city",))
    launch(service, replace(request, ask=AskMode.PROPOSE_ONLY))
    assert len(engine.calls) == 2
    assert service.hang_store.list_for("s", "o")[0].kind == "propose"


def test_schema_parsing_and_response_copy_preserve_handling():
    data = {"mode": "respond", "reply": "data", "tool_handling": [
        {"task_id": "a", "disposition": "deferred", "reason": "pending"},
        {"task_id": "b", "disposition": "success"}],
        "use_tool": True, "need": "update", "refresh_reason": "explicit request"}
    for response in (_persona_to_model_response(data, "m"), _to_model_response(data, "m")):
        assert response.tool_handling[0]["task_id"] == "a"
        assert len(response.tool_handling) == 1
        assert response.tool_intent.refresh_reason == "explicit request"
        assert replace(response, raw_text="raw").tool_handling == response.tool_handling
    assert ModelResponse("m", tool_handling=({"task_id": "a", "disposition": "deferred"},)).tool_handling


def test_explicit_missing_step_continuation_binds_to_original_work(tmp_path):
    service, _ = runtime(tmp_path)
    weather = launch(service, ToolRequest(command="weather"))
    service.work_index.create("trip", "s", "o", "itinerary", {
        "plan_id": "trip", "mode": "sequential", "steps": [
            {"step_id": "weather"}, {"step_id": "ticket"}]})
    service.work_index.bind("trip", weather, "weather")

    class Planner:
        def plan(self, intake):
            return ToolRequest(command="ticket")

    service.planner = Planner()
    intake = service.intake(subject_id="s", object_id="o", need="get remaining ticket info",
                            work_id="trip", step_id="ticket")
    service.drain_for_tests()
    row = service.work_index.get("trip")
    assert set(row["tasks"]) == {"weather", "ticket"}
    assert row["tasks"]["ticket"] == service.intake_store.get(intake.intake_id).task_id
    handle(service, list(row["tasks"].values()), "answered", "complete itinerary", True)
    assert service.work_index.get("trip")["closed"]
    # References do not authorize attaching another person's task.
    other = service.intake(subject_id="s", object_id="other", need="ticket", work_id="trip")
    service.drain_for_tests()
    assert not service.intake_store.get(other.intake_id).meta.get("work_id")


def test_pi_preserves_only_explicit_structured_coverage():
    assert _result_payload('{"coverage":{"city":"x"},"data":[1]}')["coverage"] == {"city": "x"}
    assert "coverage" not in _result_payload('some text {"city":"x"}')
    assert "coverage" not in _result_payload('{"coverage":"city x"}')


def test_restart_exposes_interrupted_step_and_allows_retry(tmp_path):
    service, _ = runtime(tmp_path)
    record = service.hang_store.create(subject_id="s", object_id="o", command="weather", need="weather")
    service.work_index.create("trip", "s", "o", "weather")
    service.work_index.bind("trip", record.task_id)
    restarted, engine = runtime(tmp_path)
    assert not engine.calls
    restarted.reap_stale("s", "o")
    assert restarted.hang_store.get(record.task_id).status == "cancelled"
    assert "进程中断" in restarted.format_tool_related_block("s", "o")
    launch(restarted, ToolRequest(command="weather"))
    assert len(engine.calls) == 1


def test_deferred_overflow_does_not_starve_unseen_results(tmp_path):
    service, _ = runtime(tmp_path)
    for i in range(14):
        launch(service, ToolRequest(command=f"query{i}"), need=f"item{i}")
    first = service.list_tool_related_entries("s", "o", query="continue")
    assert len(first) == 12
    ids = [e["id"] for e in first]
    handle(service, ids)
    service.mark_main_seen("s", "o", item_ids=ids)
    second = service.list_tool_related_entries("s", "o", query="continue")
    assert len({e["id"] for e in second} - set(ids)) == 2


def test_policy_file_enables_declared_read_only_tool(tmp_path):
    (tmp_path / "tool_reuse.json").write_text(json.dumps({
        "weather": {"read_only": True, "ttl_s": 600, "required_params": ["city"]},
        "write": {"read_only": False, "ttl_s": 600, "required_params": ["city"]},
        "infinite": {"read_only": True, "ttl_s": float("inf"), "required_params": ["city"]},
    }), encoding="utf-8")
    service, engine = runtime(tmp_path)
    assert set(service.reuse_policies) == {"weather"}
    request = ToolRequest(command="weather", params={"city": "x"})
    launch(service, request)
    launch(service, request)
    assert len(engine.calls) == 1


def test_cancel_restored_plan_persists_without_replay(tmp_path):
    service, _ = runtime(tmp_path)
    intake = service.intake_store.create(subject_id="s", object_id="o", need="plan")
    service._launch_plan(intake, ToolPlan("p", "plan", "parallel", (
        ToolStep("a", ToolRequest(command="weather")),)))
    service.drain_for_tests()
    restarted, engine = runtime(tmp_path)
    assert restarted.cancel_plan("p")
    assert not engine.calls
    assert restarted.plan_summary("p")["status"] == "cancelled"
    third, _ = runtime(tmp_path)
    assert third.plan_summary("p")["status"] == "cancelled"


def test_latest_planning_intake_never_falls_back_to_old_task(tmp_path):
    from jshi.tool.view import _pick_intake, format_tool_process
    service, _ = runtime(tmp_path)
    old = launch(service, ToolRequest(command="old"), need="old query")
    current = service.intake_store.create(subject_id="s", object_id="o", need="new query")
    intakes = service.intake_store.list_for("s", "o")
    hangs = service.hang_store.list_for("s", "o")
    chosen, hang, error = _pick_intake(intakes, hangs, "")
    assert chosen.intake_id == current.intake_id
    assert hang is None and not error
    text = format_tool_process(service, subject_id="s", object_id="o")
    assert "本次正在策划，尚无执行结果" in text
    primary = text.split("【其他尚未送入】")[0]
    assert old not in primary and "command=old" not in primary
    assert _pick_intake((), hangs, "")[1].task_id == old


def test_partial_agent_result_has_reason_and_no_false_pending_steps(tmp_path):
    service, _ = runtime(tmp_path)
    record = service.hang_store.create(subject_id="s", object_id="o", need="itinerary",
        plan_id="trip", plan_mode="agent_loop", plan_total=2)
    service.work_index.create("trip", "s", "o", "itinerary", {
        "mode": "agent_loop", "steps": [{"step_id": "weather"}, {"step_id": "guide"}]})
    service.work_index.bind("trip", record.task_id, "agent")
    service.hang_store.append_feedback(record.task_id, (ToolFeedback(kind=FeedbackKind.RESULT,
        result=ToolResult(status=ToolStatus.PARTIAL, error="工具执行超时", ideal=False,
                          result={"content": "weather and route obtained"})),))
    service.hang_store.set_wrap(record.task_id, visible=True, terminal=True,
                                summary="weather and route obtained")
    entries = service.list_tool_related_entries("s", "o")
    assert len(entries) == 1  # partial must survive deduplication
    assert entries[0]["status"] == "部分完成"
    assert entries[0]["pending_steps"] == []
    text = service.format_tool_related_block("s", "o")
    assert "# 近期已结束的工具" in text
    assert "状态：部分完成" in text and "结束原因：工具执行超时" in text
    assert "整体部分完成" in text and "第?/2步" not in text
    assert "尚未完成的步骤" not in text
    assert "weather and route obtained" in text


def test_agent_loop_receives_longer_budget_and_focus_instruction(tmp_path):
    from jshi.tool.pi_engine import PiEngine
    from jshi.tool.service import (resolve_timeout_s, TIMEOUT_USE_S,
                                  TIMEOUT_AGENT_LOOP_S, TIMEOUT_MAX_S)
    assert TIMEOUT_USE_S == 180
    assert resolve_timeout_s("agent_loop") == TIMEOUT_AGENT_LOOP_S == 600
    assert resolve_timeout_s("agent_loop", {"time_est_ms": 1_000_000}) == TIMEOUT_MAX_S
    service, engine = runtime(tmp_path)
    intake = service.intake_store.create(subject_id="s", object_id="o", need="itinerary")
    service._launch_plan(intake, ToolPlan("trip", "itinerary", "agent_loop", (
        ToolStep("weather", ToolRequest(command="weather")),
        ToolStep("guide", ToolRequest(command="guide")),)))
    service.drain_for_tests()
    assert len(engine.calls) == 1
    request = engine.calls[0]
    assert request.meta["timeout_s"] == 600
    prompt = PiEngine()._build_agent_loop_prompt(request, request.meta["agent_plan"])
    assert "不要阅读项目 README、源码、测试、Git 历史" in prompt
    assert "取得所需材料后及时汇总并结束会话" in prompt
