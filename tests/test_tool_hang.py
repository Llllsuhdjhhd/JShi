"""200 交接、记挂跟随与主链路发起（Stub；不覆盖 Pi）。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import (
    ModelRequest,
    ModelResponse,
    ResponseItem,
    ResponsePlan,
    ToolUseIntent,
)
from jshi.recognition import ObjectProfile
from jshi.skill import CognitionSkill, SkillPlanner, ToolPlanSkill, ToolWrapSkill
from jshi.subject import ActivityStatus, HistoryKind, SubjectProcess, SubjectRepository
from jshi.tool import (
    FeedbackKind,
    HangStore,
    RulePlanner,
    StubEngine,
    ToolFeedback,
    ToolModule,
    ToolProgress,
    ToolRequest,
    ToolResult,
    ToolRunner,
    ToolService,
    ToolStatus,
    load_catalog,
)


class _PlanModel:
    name = "plan-model"

    def __init__(self, plan: ResponsePlan, tool_intent: ToolUseIntent | None = None) -> None:
        self._plan = plan
        self._tool_intent = tool_intent

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(
            model=self.name,
            response_plan=self._plan,
            tool_intent=self._tool_intent,
        )


class _LatchEngine:
    """等测试放行才交出 Stub 反馈，保证策划完成后记挂仍为 open。"""

    name = "latch"

    def __init__(self) -> None:
        self.release = threading.Event()
        self._stub = StubEngine()

    def list_templates(self) -> tuple[str, ...]:
        return self._stub.list_templates()

    def execute(self, request: ToolRequest):
        self.release.wait(timeout=5)
        return self._stub.execute(request)


class _LatchPlanner:
    def __init__(self) -> None:
        self.release = threading.Event()
        self._inner = RulePlanner()

    def plan(self, intake):
        self.release.wait(timeout=5)
        return self._inner.plan(intake)


class _DummyModel:
    name = "dummy"

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        raise AssertionError("parse-only")


def _runtime(
    tmp_path: Path,
    *,
    tool_intent: ToolUseIntent | None = None,
    latch: _LatchEngine | None = None,
    planner=None,
    wrap_skill=None,
    engine=None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    hang_store = HangStore(tmp_path / "hang.jsonl")
    tool_engine = engine or latch or StubEngine()
    runner = ToolRunner(ToolModule(tool_engine), hang_store)
    service = ToolService(
        hang_store,
        runner,
        planner=planner,
        wrap_skill=wrap_skill,
        intake_path=tmp_path / "tool.jsonl",
    )
    plan = ResponsePlan(
        mode="respond",
        reason="去查",
        items=(ResponseItem(channel="verbal", text="我去查一下"),),
    )
    process = SubjectProcess(
        repository,
        identities,
        _PlanModel(plan, tool_intent=tool_intent),
        tool_service=service,
    )
    process.profiles.create(
        ObjectProfile(object_id="OBJ-USER", label="user", source="test", status="confirmed")
    )
    return process, repository, hang_store, service


def test_hang_stub_feedback_notifies_on_result(tmp_path: Path) -> None:
    store = HangStore(tmp_path / "hang.jsonl")
    runner = ToolRunner(ToolModule(StubEngine()), store)
    record = store.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="查一下天气",
        template="echo",
        field_ref={"activity_id": "act-1", "zone_kind": "wood", "zone_rev": "1"},
    )
    assert store.list_open("stone", "OBJ-A")[0].task_id == record.task_id
    runner.start(
        ToolRequest(
            request_id=record.request_id,
            subject_id="stone",
            activity_id="act-1",
            need="查一下天气",
            template="echo",
        ),
        record.task_id,
    )
    runner.drain_for_tests()
    done = store.get(record.task_id)
    assert done is not None
    kinds = [item.kind for item in done.feedback]
    assert kinds == [FeedbackKind.ESTIMATE, FeedbackKind.PROGRESS, FeedbackKind.RESULT]
    assert done.visible is True
    assert done.status == "notified"
    assert done.summary


def test_list_open_filters_by_object(tmp_path: Path) -> None:
    store = HangStore(tmp_path / "hang.jsonl")
    store.create(subject_id="stone", object_id="OBJ-A", need="查 A")
    store.create(subject_id="stone", object_id="OBJ-B", need="查 B")
    open_a = store.list_open("stone", "OBJ-A")
    assert len(open_a) == 1
    assert open_a[0].object_id == "OBJ-A"
    assert all(item.object_id != "OBJ-B" for item in open_a)


def test_cancel_drops_from_open_list(tmp_path: Path) -> None:
    store = HangStore(tmp_path / "hang.jsonl")
    record = store.create(subject_id="stone", object_id="OBJ-A", need="查一下")
    store.cancel(record.task_id)
    assert store.list_open("stone", "OBJ-A") == ()
    cancelled = store.get(record.task_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


def test_intake_is_not_hang_until_plan_succeeds(tmp_path: Path) -> None:
    planner = _LatchPlanner()
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=planner,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    assert result.activity.status == ActivityStatus.COMPLETED
    object_id = result.speaker.object_id
    assert store.list_open("stone", object_id) == ()
    intakes = service.intake_store.list_for("stone", object_id)
    assert len(intakes) == 1
    assert intakes[0].status == "received"
    planner.release.set()
    service.drain_planning_for_tests()
    records = store.list_for("stone", object_id)
    assert len(records) == 1
    assert records[0].need == "查一下天气"
    service.drain_for_tests()


def test_process_starts_hang_and_still_speaks(tmp_path: Path) -> None:
    latch = _LatchEngine()
    process, repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        latch=latch,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    assert result.activity.status == ActivityStatus.COMPLETED
    assert result.action_text == "我去查一下"
    facts = [
        item
        for item in repository.list_history("stone", HistoryKind.FACT)
        if item.event_type == "language_action"
    ]
    assert facts[0].content["text"] == "我去查一下"
    service.drain_planning_for_tests()
    opened = store.list_open("stone", result.speaker.object_id)
    assert len(opened) == 1
    assert opened[0].need == "查一下天气"
    assert opened[0].field_ref.get("zone_kind") == "wood"
    assert "rewritten_context" not in opened[0].field_ref
    kinds_before = [item.kind for item in opened[0].feedback]
    assert FeedbackKind.ESTIMATE in kinds_before
    latch.release.set()
    service.drain_for_tests()
    done = store.get(opened[0].task_id)
    assert done is not None
    kinds = [item.kind for item in done.feedback]
    assert FeedbackKind.ESTIMATE in kinds
    assert FeedbackKind.PROGRESS in kinds
    assert FeedbackKind.RESULT in kinds
    assert done.visible is True
    assert done.status == "notified"
    visible = service.list_visible("stone", result.speaker.object_id)
    assert len(visible) == 1
    assert visible[0].id == done.task_id
    assert visible[0].summary == done.summary


def test_process_without_tool_intent_leaves_hang_empty(tmp_path: Path) -> None:
    process, _repository, store, service = _runtime(tmp_path, tool_intent=None)
    result = process.experience("stone", "你好", object_ref="user")
    service.drain_for_tests()
    assert result.activity.status == ActivityStatus.COMPLETED
    assert store.list_open("stone", result.speaker.object_id) == ()
    assert not (tmp_path / "hang.jsonl").is_file()
    assert not (tmp_path / "tool.jsonl").is_file()
    assert service.list_visible("stone", result.speaker.object_id) == ()


def test_plan_failure_has_no_hang_but_is_visible(tmp_path: Path) -> None:
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="x"),
    )
    result = process.experience("stone", "查一下", object_ref="user")
    service.drain_for_tests()
    object_id = result.speaker.object_id
    assert result.activity.status == ActivityStatus.COMPLETED
    assert store.list_open("stone", object_id) == ()
    assert not (tmp_path / "hang.jsonl").is_file()
    visible = service.list_visible("stone", object_id)
    assert len(visible) == 1
    assert visible[0].kind == "plan_failed"
    assert "过短" in visible[0].summary


def test_list_visible_filters_by_object(tmp_path: Path) -> None:
    process, _repository, _store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    object_id = result.speaker.object_id
    assert service.list_visible("stone", object_id)
    assert service.list_visible("stone", "OBJ-OTHER") == ()


def test_cancel_hides_from_list_visible(tmp_path: Path) -> None:
    process, _repository, _store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    object_id = result.speaker.object_id
    visible = service.list_visible("stone", object_id)
    assert len(visible) == 1
    assert service.cancel(visible[0].id) is True
    assert service.list_visible("stone", object_id) == ()


def test_assembly_includes_visible_tool_input(tmp_path: Path) -> None:
    process, _repository, _store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
    )
    first = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    process.cognition = _PlanModel(
        ResponsePlan(
            mode="respond",
            reason="回话",
            items=(ResponseItem(channel="verbal", text="好"),),
        ),
        tool_intent=None,
    )
    second = process.experience("stone", "还在吗", object_ref="user")
    visible = service.list_visible("stone", second.speaker.object_id)
    assert visible
    assert second.current_state.tool_input == visible[0].summary
    assert all(item.source != "tool" for item in second.current_state.fragments)
    assert first.activity.status == ActivityStatus.COMPLETED


def test_assembly_omits_tool_when_nothing_visible(tmp_path: Path) -> None:
    process, _repository, _store, service = _runtime(tmp_path, tool_intent=None)
    result = process.experience("stone", "你好", object_ref="user")
    service.drain_for_tests()
    assert result.current_state.tool_input == ""
    assert all(item.source != "tool" for item in result.current_state.fragments)


def test_cognition_parses_use_tool_and_need() -> None:
    skill = CognitionSkill(_DummyModel())
    parsed = skill.parse(
        {
            "response_plan": {
                "mode": "respond",
                "reason": "去查",
                "items": [{"channel": "verbal", "text": "我去查一下"}],
            },
            "use_tool": True,
            "need": "查天气",
            "tool_request": {
                "need": "旧键应忽略",
                "template": "echo",
                "params": {"value": "ok"},
                "expected_result": "一句天气",
            },
        }
    )
    assert parsed.tool_intent is not None
    assert parsed.tool_intent.need == "查天气"
    assert getattr(parsed, "tool_request", None) is None


def test_cognition_parses_short_need_when_use_tool() -> None:
    skill = CognitionSkill(_DummyModel())
    short = skill.parse(
        {
            "response_plan": {"mode": "think", "items": []},
            "use_tool": True,
            "need": "x",
        }
    )
    assert short.tool_intent is not None
    assert short.tool_intent.need == "x"


def test_cognition_ignores_old_tool_request_and_broken() -> None:
    skill = CognitionSkill(_DummyModel())
    old = skill.parse(
        {
            "response_plan": {"mode": "think", "items": []},
            "tool_request": {"need": "查天气", "template": "echo"},
        }
    )
    assert old.tool_intent is None
    blank = skill.parse(
        {
            "response_plan": {"mode": "think", "items": []},
            "use_tool": True,
            "need": " ",
        }
    )
    assert blank.tool_intent is not None
    assert blank.tool_intent.need == ""
    omitted = skill.parse({"response_plan": {"mode": "think", "items": []}})
    assert omitted.tool_intent is None
    false_flag = skill.parse(
        {
            "response_plan": {"mode": "think", "items": []},
            "use_tool": False,
            "need": "查天气",
        }
    )
    assert false_flag.tool_intent is None


def test_instruction_mentions_use_tool_not_template() -> None:
    assert "use_tool" in CognitionSkill.instruction
    assert "在合适的时候告诉对方" in CognitionSkill.instruction
    assert "【在途工具】" not in CognitionSkill.instruction
    assert "tool_request" not in CognitionSkill.instruction
    schema = json.dumps(CognitionSkill.schema, ensure_ascii=False)
    assert "use_tool" in schema
    assert "template" not in schema
    assert "tool_request" not in schema


class _JsonPort:
    name = "json-port"

    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = 0

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        if callable(self.payload):
            text = self.payload(self.calls)
        elif isinstance(self.payload, str):
            text = self.payload
        else:
            text = json.dumps(self.payload, ensure_ascii=False)
        return ModelResponse(
            model=self.name,
            response_plan=ResponsePlan(
                mode="respond",
                items=(ResponseItem(channel="verbal", text=text),),
            ),
            raw_text=text,
        )


class _BoomWrap:
    model_tag = "boom@v1"

    def wrap_hang(self, hang, batch):
        del hang, batch
        raise RuntimeError("boom")


class _StreamEngine:
    name = "stream"

    def __init__(self) -> None:
        self.after_progress = threading.Event()
        self.go_result = threading.Event()

    def list_templates(self):
        return ("echo",)

    def iter_execute(self, request: ToolRequest):
        yield ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.PROGRESS,
            progress=ToolProgress(stage="running", partial="阶段一完成"),
        )
        self.after_progress.set()
        self.go_result.wait(timeout=5)
        yield ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.RESULT,
            result=ToolResult(status=ToolStatus.OK, summary="全部完成"),
        )

    def execute(self, request: ToolRequest):
        return tuple(self.iter_execute(request))


def test_visible_false_omitted_from_list(tmp_path: Path) -> None:
    wrap = ToolWrapSkill(_JsonPort({"visible": False, "summary": ""}))
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        wrap_skill=wrap,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    done = store.list_for("stone", result.speaker.object_id)[0]
    assert done.visible is False
    assert service.list_visible("stone", result.speaker.object_id) == ()


def test_wrap_skill_summary_not_engine_payload(tmp_path: Path) -> None:
    wrap = ToolWrapSkill(_JsonPort({"visible": True, "summary": "天气已经查到"}))
    process, _repository, _store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        wrap_skill=wrap,
    )
    first = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    process.cognition = _PlanModel(
        ResponsePlan(
            mode="respond",
            reason="回话",
            items=(ResponseItem(channel="verbal", text="好"),),
        )
    )
    second = process.experience("stone", "还在吗", object_ref="user")
    assert second.current_state.tool_input == "天气已经查到"
    assert "echo:" not in second.current_state.tool_input
    assert first.activity.status == ActivityStatus.COMPLETED


def test_wrap_twice_keeps_one_visible_line(tmp_path: Path) -> None:
    port = _JsonPort(
        lambda n: json.dumps(
            {"visible": True, "summary": f"第{n}句"},
            ensure_ascii=False,
        )
    )
    wrap = ToolWrapSkill(port)
    process, _repository, _store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        wrap_skill=wrap,
        engine=_StreamEngine(),
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_planning_for_tests()
    engine = process.tool_runner.module.engine
    assert isinstance(engine, _StreamEngine)
    engine.after_progress.wait(timeout=5)
    engine.go_result.set()
    service.drain_for_tests()
    visible = service.list_visible("stone", result.speaker.object_id)
    assert len(visible) == 1
    assert visible[0].summary == f"第{port.calls}句"
    assert port.calls >= 2


def test_list_visible_does_not_leak_objects(tmp_path: Path) -> None:
    process, _repository, _store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    other = process.profiles.create(
        ObjectProfile(object_id="OBJ-B", label="other", source="test", status="confirmed")
    )
    del other
    assert service.list_visible("stone", "OBJ-B") == ()
    assert service.list_visible("stone", result.speaker.object_id)


def test_second_intake_while_first_still_planning(tmp_path: Path) -> None:
    planner = _LatchPlanner()
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=planner,
    )
    first = process.experience("stone", "明天天气怎样", object_ref="user")
    second = process.experience("stone", "再查一次", object_ref="user")
    object_id = first.speaker.object_id
    intakes = service.intake_store.list_for("stone", object_id)
    assert len(intakes) == 2
    assert store.list_for("stone", object_id) == ()
    planner.release.set()
    service.drain_for_tests()
    assert len(store.list_for("stone", object_id)) == 2
    assert second.activity.status == ActivityStatus.COMPLETED


def test_cancel_one_hang_leaves_the_other(tmp_path: Path) -> None:
    planner = _LatchPlanner()
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=planner,
    )
    process.experience("stone", "明天天气怎样", object_ref="user")
    process.experience("stone", "再查一次", object_ref="user")
    planner.release.set()
    service.drain_for_tests()
    object_id = process.profiles.find_by_names("user")[0].object_id
    hangs = store.list_for("stone", object_id)
    assert len(hangs) == 2
    service.cancel(hangs[0].task_id)
    visible = service.list_visible("stone", object_id)
    assert len(visible) == 1
    assert visible[0].id == hangs[1].task_id


def test_wrap_error_keeps_result_and_fallback_line(tmp_path: Path) -> None:
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        wrap_skill=_BoomWrap(),
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    done = store.list_for("stone", result.speaker.object_id)[0]
    assert any(item.kind is FeedbackKind.RESULT for item in done.feedback)
    visible = service.list_visible("stone", result.speaker.object_id)
    assert len(visible) == 1
    assert "boom" not in visible[0].summary


def test_stream_engine_writes_progress_before_result(tmp_path: Path) -> None:
    store = HangStore(tmp_path / "hang.jsonl")
    engine = _StreamEngine()
    runner = ToolRunner(ToolModule(engine), store)
    record = store.create(subject_id="stone", object_id="OBJ-A", need="查一下")
    runner.start(
        ToolRequest(request_id=record.request_id, need="查一下", template="echo"),
        record.task_id,
    )
    assert engine.after_progress.wait(timeout=5)
    mid = store.get(record.task_id)
    assert mid is not None
    kinds = [item.kind for item in mid.feedback]
    assert FeedbackKind.PROGRESS in kinds
    assert FeedbackKind.RESULT not in kinds
    engine.go_result.set()
    runner.drain_for_tests()
    done = store.get(record.task_id)
    assert done is not None
    assert FeedbackKind.RESULT in [item.kind for item in done.feedback]


def test_plan_skill_parse_failure_has_no_hang(tmp_path: Path) -> None:
    planner = SkillPlanner(ToolPlanSkill(_JsonPort("not-json")), catalog=load_catalog())
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=planner,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    assert store.list_for("stone", result.speaker.object_id) == ()
    visible = service.list_visible("stone", result.speaker.object_id)
    assert len(visible) == 1
    assert visible[0].kind == "plan_failed"


def test_plan_skill_rejects_unknown_template(tmp_path: Path) -> None:
    planner = SkillPlanner(
        ToolPlanSkill(_JsonPort({"ok": True, "template": "not-a-template"})),
        catalog=load_catalog(),
    )
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=planner,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    assert store.list_for("stone", result.speaker.object_id) == ()
    visible = service.list_visible("stone", result.speaker.object_id)
    assert visible
    assert "目录" in visible[0].summary


def test_plan_skill_ok_creates_hang(tmp_path: Path) -> None:
    planner = SkillPlanner(
        ToolPlanSkill(
            _JsonPort(
                {
                    "ok": True,
                    "template": "echo",
                    "params": {},
                    "estimate": {
                        "benefit": "回显需求",
                        "downside": "占位",
                    },
                }
            )
        ),
        catalog=load_catalog(),
    )
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=planner,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    hangs = store.list_for("stone", result.speaker.object_id)
    assert len(hangs) == 1
    assert hangs[0].template == "echo"
    visible = service.list_visible("stone", result.speaker.object_id)
    assert len(visible) == 1
    assert visible[0].kind == "hang"


def test_catalog_slots_include_time_and_exception() -> None:
    items = load_catalog()
    names = {str(item.get("name") or "") for item in items}
    assert "echo" in names
    assert "generic" in names
    for item in items:
        assert item.get("time_note")
        assert item.get("exception_note")


def test_pi_engine_drops_unmapped_events_and_stderr_is_devnull() -> None:
    from jshi.tool.pi_engine import PiEngine
    from jshi.tool.contract import ToolRequest

    engine = PiEngine()
    converted = engine._convert_event(
        ToolRequest(need="x"),
        {"type": "agent_thought", "text": "内部思维"},
    )
    assert converted is None
    progress = engine._convert_event(
        ToolRequest(need="x"),
        {
            "type": "tool_execution_update",
            "toolName": "echo",
            "partialResult": {"content": [{"type": "text", "text": "半段"}]},
        },
    )
    assert progress is not None
    assert progress.progress is not None
    assert progress.progress.partial == "半段"
    assert engine._spawn.__func__ is PiEngine._spawn
    # 源码约定 stderr=DEVNULL，避免 PIPE 堵死。
    import inspect

    source = inspect.getsource(PiEngine._spawn)
    assert "DEVNULL" in source
