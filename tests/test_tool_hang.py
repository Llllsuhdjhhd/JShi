"""200 交接、记挂跟随与主链路发起（Stub；不覆盖 Pi）。"""

from __future__ import annotations

import io
import json
import threading
import time
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
    AskMode,
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
    engine_tools,
    load_catalog,
)
from jshi.tool.hang import HangRecord, NOTE_MAX_CHARS
from jshi.tool.plan import ToolPlan, ToolStep


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
    assert store.list_for("stone", "OBJ-A")[0].task_id == record.task_id
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
    assert kinds == [FeedbackKind.PROGRESS, FeedbackKind.RESULT]
    assert done.visible is True
    assert done.status == "notified"
    assert done.summary


def test_cancel_marks_hang_cancelled(tmp_path: Path) -> None:
    store = HangStore(tmp_path / "hang.jsonl")
    record = store.create(subject_id="stone", object_id="OBJ-A", need="查一下")
    store.cancel(record.task_id)
    cancelled = store.get(record.task_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


def test_terminal_wrap_notifies_even_when_not_visible(tmp_path: Path) -> None:
    """终态包装必须收口；visible=false 时也不许一直停在 open。"""
    store = HangStore(tmp_path / "hang.jsonl")
    record = store.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="查果蝇",
        command="skill:web-search",
    )
    store.set_wrap(
        record.task_id,
        visible=True,
        summary="中间进展一句",
        terminal=False,
    )
    assert store.get(record.task_id).status == "open"
    store.set_wrap(
        record.task_id,
        visible=False,
        summary="终态摘要：已取回一批结果",
        terminal=True,
    )
    done = store.get(record.task_id)
    assert done is not None
    assert done.status == "notified"
    assert done.visible is False
    assert "已取回一批结果" in done.summary


def test_hang_rejects_feedback_after_cancel(tmp_path: Path) -> None:
    store = HangStore(tmp_path / "hang.jsonl")
    record = store.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="查天气",
        template="echo",
    )
    store.cancel(record.task_id)
    updated = store.append_feedback(
        record.task_id,
        (
            ToolFeedback(
                request_id=record.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(status=ToolStatus.OK),
            ),
        ),
    )
    assert updated is None
    assert store.get(record.task_id).feedback == ()


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
    assert store.list_for("stone", object_id) == ()
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
    opened = store.list_for("stone", result.speaker.object_id)
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
    phases = {stage.phase for stage in done.stages}
    assert {"planning", "estimate", "result"}.issubset(phases)
    visible = service.list_visible("stone", result.speaker.object_id)
    assert len(visible) == 1
    assert visible[0].id == done.task_id
    assert visible[0].summary == done.summary


def test_process_without_tool_intent_leaves_hang_empty(tmp_path: Path) -> None:
    process, _repository, store, service = _runtime(tmp_path, tool_intent=None)
    result = process.experience("stone", "你好", object_ref="user")
    service.drain_for_tests()
    assert result.activity.status == ActivityStatus.COMPLETED
    assert store.list_for("stone", result.speaker.object_id) == ()
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
    assert store.list_for("stone", object_id) == ()
    assert not (tmp_path / "hang.jsonl").is_file()
    visible = service.list_visible("stone", object_id)
    assert len(visible) == 1
    assert visible[0].kind == "plan_failed"
    assert "太短" in visible[0].summary


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
    object_id = first.speaker.object_id
    before = service.list_visible("stone", object_id)
    assert before
    summary = before[0].summary
    second = process.experience("stone", "还在吗", object_ref="user")
    related = second.current_state.tool_input
    assert "【工具相关】" in related
    assert summary in related
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
    assert "找合适时机告诉对方" in CognitionSkill.instruction
    assert "【工具相关】" in CognitionSkill.instruction
    assert "【在途工具】" not in CognitionSkill.instruction
    assert "tool_request" not in CognitionSkill.instruction
    schema = json.dumps(CognitionSkill.schema, ensure_ascii=False)
    assert "use_tool" in schema
    assert "template" not in schema
    assert "tool_request" not in schema


def test_tool_wording_is_shared_by_all_three_instructions() -> None:
    """三段提示词（木头 + 两个共用同一段的），口径必须一致：

    去掉范围糊的「不要无故再开同样一条」；补两个例子。
    2026-09-13 收窄：原来只写「不得因为前一次的工具使用对本次的判断造成影响」，
    字面上等于让 05 无视上一次——实测里它因此对着同一个工具反复发起。
    现在拆成两条：失败不迷信、成功不无视。
    """
    from jshi.style import reply_instruction_for

    texts = {
        "wood": CognitionSkill.instruction,
        "suxipo": reply_instruction_for("suxipo"),
        "smith": reply_instruction_for("smith"),
    }
    for name, text in texts.items():
        assert "不要无故再开同样一条" not in text, name
        assert "若本轮有【工具热状态】" not in text, name
        assert "【工具相关】" in text, name
        assert "找合适时机告诉对方" in text, name
        assert "【回话】" not in text, name
        assert "暂缓告知" in text, name
        assert "怎么对对象开口" in text or "见任务1" in text, name
        assert "在做是否使用工具的判断时" not in text, name
        assert "不得因为前一次失败就断定这次也不行" in text, name
        assert "也不得无视前一次已经成功" in text, name
        assert "不得因为天气查询失败而退出不能询问机票" in text, name
        assert "要根据时间合理推算时效性" in text, name
        assert "【任务2 · 工具】" in text, name
        assert "不需要工具则不要这两个键" in text, name
    # 人格两份共用同一段（改一处两个人格同时生效）
    from jshi.style.packs import PERSONA_TOOL_NOTE, TOOL_TASK_NOTE

    assert TOOL_TASK_NOTE in texts["wood"]
    assert PERSONA_TOOL_NOTE in texts["suxipo"]
    assert PERSONA_TOOL_NOTE in texts["smith"]
    assert PERSONA_TOOL_NOTE == TOOL_TASK_NOTE


class _JsonPort:
    name = "json-port"

    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = 0
        self.last_request: ModelRequest | None = None

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.last_request = request
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

    def iter_execute(self, request: ToolRequest, cancel=None):
        del cancel
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


def test_tool_wrap_selects_prompt_by_stage() -> None:
    wrap = ToolWrapSkill(_JsonPort({"visible": True, "summary": "一句"}))
    hang = HangRecord(
        task_id="task-1",
        request_id="req-1",
        subject_id="stone",
        object_id="OBJ-A",
        need="查天气",
        command="echo",
        kind="use",
    )
    result_item = ToolFeedback(
        request_id="req-1",
        kind=FeedbackKind.RESULT,
        result=ToolResult(status=ToolStatus.OK, summary="完成"),
    )
    prompt, _payload = wrap._prompt_and_payload(hang, (result_item,))
    assert "最终结果" in prompt

    progress_item = ToolFeedback(
        request_id="req-1",
        kind=FeedbackKind.PROGRESS,
        progress=ToolProgress(stage="search", partial="正在搜索"),
    )
    prompt, _payload = wrap._prompt_and_payload(hang, (progress_item,))
    assert "阶段性进展" in prompt

    prompt, _payload = wrap._prompt_and_payload(hang, ())
    assert "工具策划" in prompt


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
    related = second.current_state.tool_input
    assert "【工具相关】" in related
    assert "天气已经查到" in related
    assert "echo:" not in related
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
    """策划未完成时两本交接可并存；同 command 启动时第二本并入，不新建记挂。"""
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
    # RulePlanner 两本都是 echo → command 硬闸并成一本
    assert len(store.list_for("stone", object_id)) == 1
    assert second.activity.status == ActivityStatus.COMPLETED


def test_intake_dedupes_same_need_when_hang_open(tmp_path: Path) -> None:
    hang_store = HangStore(tmp_path / "hang.jsonl")
    runner = ToolRunner(ToolModule(StubEngine()), hang_store)
    service = ToolService(
        hang_store,
        runner,
        planner=RulePlanner(),
        intake_path=tmp_path / "tool.jsonl",
    )
    open_hang = hang_store.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="查一下天气",
        command="echo",
    )
    assert open_hang.status == "open"
    again = service.intake(
        subject_id="stone",
        object_id="OBJ-A",
        need="查一下天气",
        verbal="再查",
    )
    assert again.status == "launched"
    assert again.meta.get("dedupe") == "need"
    assert again.task_id == open_hang.task_id
    assert len(hang_store.list_for("stone", "OBJ-A")) == 1


def test_cancel_one_hang_leaves_the_other(tmp_path: Path) -> None:
    class _TwoCommandPlanner:
        def __init__(self) -> None:
            self.release = threading.Event()
            self._n = 0

        def plan(self, intake):
            self.release.wait(timeout=5)
            self._n += 1
            command = "echo" if self._n == 1 else "llama"
            return ToolRequest(
                subject_id=intake.subject_id,
                activity_id=intake.activity_id,
                need=intake.need,
                template="generic",
                command=command,
                ask=AskMode.EXECUTE,
            )

    planner = _TwoCommandPlanner()
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


def test_plan_ignores_model_template_and_uses_default(tmp_path: Path) -> None:
    """定义模板不做选择：模型给什么模板名都不算数，一律落默认 generic。"""
    planner = SkillPlanner(
        ToolPlanSkill(_JsonPort({"ok": True, "template": "not-a-template", "command": "echo"})),
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
    assert hangs[0].template == "generic"


def test_plan_skill_ok_creates_hang(tmp_path: Path) -> None:
    planner = SkillPlanner(
        ToolPlanSkill(
            _JsonPort(
                {
                    "ok": True,
                    "template": "echo",
                    "command": "echo",
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
    assert hangs[0].template == "generic"
    assert hangs[0].meta.get("plan_model_tag") == "json-port@v1"
    intakes = service.intake_store.list_for("stone", result.speaker.object_id)
    assert intakes[0].meta.get("model_tag") == "json-port@v1"
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


class _CaptureEngine(StubEngine):
    def __init__(self) -> None:
        super().__init__()
        self.last: ToolRequest | None = None

    def execute(self, request: ToolRequest):
        self.last = request
        return super().execute(request)


class _BlockingStdout:
    def __init__(self) -> None:
        self._closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self) -> str:
        self._closed.wait(timeout=30)
        raise StopIteration

    def close(self) -> None:
        self._closed.set()


class _FakePopen:
    def __init__(self, lines: tuple[str, ...] = (), *, hang: bool = False) -> None:
        self.stdin = io.StringIO()
        self.returncode = None
        self.stdout = _BlockingStdout() if hang else iter(lines)

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        closer = getattr(self.stdout, "close", None)
        if closer is not None:
            closer()

    def terminate(self) -> None:
        self.kill()


def test_service_writes_exactly_one_estimate(tmp_path: Path) -> None:
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    hang = store.list_for("stone", result.speaker.object_id)[0]
    kinds = [item.kind for item in hang.feedback]
    assert kinds.count(FeedbackKind.ESTIMATE) == 1
    assert kinds[0] is FeedbackKind.ESTIMATE


def test_wrap_truncates_long_summary(tmp_path: Path) -> None:
    wrap = ToolWrapSkill(_JsonPort({"visible": True, "summary": "啊" * 200}))
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        wrap_skill=wrap,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    done = store.list_for("stone", result.speaker.object_id)[0]
    assert len(done.summary) == NOTE_MAX_CHARS
    visible = service.list_visible("stone", result.speaker.object_id)
    assert visible
    assert len(visible[0].summary) == NOTE_MAX_CHARS


def test_progress_wrap_keeps_status_open(tmp_path: Path) -> None:
    wrap = ToolWrapSkill(_JsonPort({"visible": True, "summary": "还在查"}))
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        wrap_skill=wrap,
        engine=_StreamEngine(),
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_planning_for_tests()
    engine = process.tool_runner.module.engine
    assert isinstance(engine, _StreamEngine)
    assert engine.after_progress.wait(timeout=5)
    deadline = time.monotonic() + 3
    mid = None
    while time.monotonic() < deadline:
        hangs = store.list_for("stone", result.speaker.object_id)
        if hangs and hangs[0].visible:
            mid = hangs[0]
            break
        time.sleep(0.05)
    assert mid is not None
    assert mid.status == "open"
    assert service.list_visible("stone", result.speaker.object_id)
    engine.go_result.set()
    service.drain_for_tests()
    done = store.get(mid.task_id)
    assert done is not None
    assert done.status == "notified"


def test_hang_jsonl_is_append_only(tmp_path: Path) -> None:
    path = tmp_path / "hang.jsonl"
    store = HangStore(path)
    record = store.create(subject_id="stone", object_id="OBJ-A", need="查一下")
    store.set_wrap(record.task_id, visible=True, summary="一句", terminal=True)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 2
    loaded = HangStore(path)
    got = loaded.get(record.task_id)
    assert got is not None
    assert got.visible is True
    assert got.status == "notified"
    assert got.summary == "一句"


def test_plan_skill_rejects_unknown_command(tmp_path: Path) -> None:
    planner = SkillPlanner(
        ToolPlanSkill(
            _JsonPort({"ok": True, "template": "echo", "command": "not-a-command"})
        ),
        catalog=load_catalog(),
        engine_tools=({"name": "echo", "description": "回显"},),
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
    assert "引擎目录" in visible[0].summary


def test_plan_never_auto_fills_the_only_command(tmp_path: Path) -> None:
    """目录只有一条也不许替模型兜底：它没说选哪个，就不算选中。

    旧程序会在这里拿那条唯一的命令去执行。那正是「模型说要造工具，
    程序却去跑一个不相干的工具」的来源。
    """
    engine = _CaptureEngine()
    planner = SkillPlanner(
        ToolPlanSkill(_JsonPort({"ok": True, "template": "generic"})),
        catalog=load_catalog(),
        engine_tools=engine_tools(engine),
    )
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=planner,
        engine=engine,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    assert store.list_for("stone", result.speaker.object_id) == ()
    assert engine.last is None                      # 没有执行任何工具
    visible = service.list_visible("stone", result.speaker.object_id)
    assert visible
    assert "该用哪个工具" in visible[0].summary


def test_propose_only_does_not_run_until_approved(tmp_path: Path) -> None:
    """propose_only 只报价、不执行；approve 后才真正交给引擎。"""
    engine = _CaptureEngine()

    class _ProposePlanner:
        def plan(self, intake):
            del intake
            return ToolRequest(
                need="查一下天气",
                template="generic",
                command="echo",
                params={},
                ask=AskMode.PROPOSE_ONLY,
                meta={
                    "estimate": {
                        "benefit": "拿到天气",
                        "downside": "可能较慢",
                        "need_confirm": True,
                    }
                },
            )

    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=_ProposePlanner(),
        engine=engine,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_planning_for_tests()
    hangs = store.list_for("stone", result.speaker.object_id)
    assert len(hangs) == 1
    assert hangs[0].visible is True
    assert "提议" in hangs[0].summary
    assert engine.last is None
    assert not any(item.kind is FeedbackKind.RESULT for item in hangs[0].feedback)

    assert service.approve(hangs[0].task_id) is True
    service.drain_for_tests()
    after = store.get(hangs[0].task_id)
    assert after is not None
    assert engine.last is not None
    assert engine.last.ask is AskMode.EXECUTE
    assert any(item.kind is FeedbackKind.RESULT for item in after.feedback)


def test_tool_plan_launches_independent_steps_in_parallel(tmp_path: Path) -> None:
    class _PlanPlanner:
        def plan(self, intake):
            return ToolPlan(
                plan_id="plan-1",
                need=intake.need,
                mode="parallel",
                steps=(
                    ToolStep(
                        step_id="a",
                        request=ToolRequest(
                            need="查天气",
                            template="generic",
                            command="echo",
                            params={},
                            ask=AskMode.EXECUTE,
                        ),
                    ),
                    ToolStep(
                        step_id="b",
                        request=ToolRequest(
                            need="查天气",
                            template="generic",
                            command="echo",
                            params={},
                            ask=AskMode.EXECUTE,
                        ),
                    ),
                ),
            )

    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=_PlanPlanner(),
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    hangs = store.list_for("stone", result.speaker.object_id)
    assert len(hangs) == 2
    assert all(hang.kind == "use" for hang in hangs)


def test_tool_plan_summary_and_cancel(tmp_path: Path) -> None:
    class _PlanPlanner:
        def plan(self, intake):
            return ToolPlan(
                plan_id="plan-2",
                need=intake.need,
                mode="parallel",
                steps=(
                    ToolStep(
                        step_id="a",
                        request=ToolRequest(
                            need="查天气",
                            template="generic",
                            command="echo",
                            params={},
                            ask=AskMode.EXECUTE,
                        ),
                    ),
                    ToolStep(
                        step_id="b",
                        request=ToolRequest(
                            need="查天气",
                            template="generic",
                            command="echo",
                            params={},
                            ask=AskMode.EXECUTE,
                        ),
                    ),
                ),
            )

    process, _repository, _store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=_PlanPlanner(),
    )
    process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    summary = service.plan_summary("plan-2")
    assert summary is not None
    assert len(summary["steps"]) == 2
    assert service.cancel_plan("plan-2") is True


def test_plan_create_tool_reaches_the_engine(tmp_path: Path) -> None:
    """没有现成工具、模型判明要造：交一份造工具规格，200 照常建记挂并跟随。"""
    engine = StubEngine()
    planner = SkillPlanner(
        ToolPlanSkill(
            _JsonPort(
                {
                    "ok": True,
                    "ask": "create_tool",
                    "create_tool": {
                        "tool_name": "weather",
                        "tool_intent": "查指定城市的天气",
                        "params_schema": {"city": {"type": "string"}},
                        "expected_output": "一句天气",
                    },
                }
            )
        ),
        catalog=load_catalog(),
        engine_tools=engine_tools(engine),
    )
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=planner,
        engine=engine,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    hangs = store.list_for("stone", result.speaker.object_id)
    assert len(hangs) == 1
    assert hangs[0].kind == "create"
    assert hangs[0].command == "weather"
    assert hangs[0].template == "generic"
    assert any(item.kind is FeedbackKind.RESULT for item in hangs[0].feedback)
    # 引擎确实走的是「造」，不是「用」
    assert [item["name"] for item in engine.list_commands()] == ["echo", "weather"]


def test_created_tool_enters_the_catalog_for_the_next_planning(tmp_path: Path) -> None:
    """造完必须刷新目录，否则同一会话里检索不到，会重复造。

    这条链的证据在断言本身：第二次策划点名 weather，而 weather 是第一轮
    才造出来的。若目录没刷新，这个名字不在名单里，策划会失败、第二本记挂
    根本不会建出来。
    """
    engine = StubEngine()
    port = _JsonPort(
        lambda n: json.dumps(
            {
                "ok": True,
                "ask": "create_tool",
                "create_tool": {"tool_name": "weather", "tool_intent": "查指定城市的天气"},
            }
            if n == 1
            else {"ok": True, "command": "weather", "params": {}},
            ensure_ascii=False,
        )
    )
    # 不给 engine_tools：要让它自己去引擎读，才谈得上「刷新」。
    planner = SkillPlanner(ToolPlanSkill(port), catalog=load_catalog(), engine=engine)
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=planner,
        engine=engine,
    )
    first = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    assert len(store.list_for("stone", first.speaker.object_id)) == 1

    second = process.experience("stone", "再查一次天气", object_ref="user")
    service.drain_for_tests()
    assert len(store.list_for("stone", second.speaker.object_id)) == 2


def test_pi_engine_streams_recorded_events() -> None:
    from jshi.tool.pi_engine import PiEngine

    lines = (
        json.dumps(
            {
                "type": "tool_execution_update",
                "toolName": "echo",
                "partialResult": {"content": [{"type": "text", "text": "半段"}]},
            },
            ensure_ascii=False,
        )
        + "\n",
        json.dumps(
            {
                "type": "tool_execution_end",
                "toolName": "echo",
                "isError": False,
                "result": {"content": [{"type": "text", "text": "完成"}]},
            },
            ensure_ascii=False,
        )
        + "\n",
        json.dumps({"type": "agent_settled"}, ensure_ascii=False) + "\n",
    )
    engine = PiEngine()
    engine._spawn = lambda: _FakePopen(lines)
    kinds = []
    progress_before_result = False
    saw_progress = False
    results = []
    for item in engine.iter_execute(ToolRequest(need="x", command="echo")):
        kinds.append(item.kind)
        if item.kind is FeedbackKind.PROGRESS:
            saw_progress = True
        if item.kind is FeedbackKind.RESULT:
            progress_before_result = saw_progress
            results.append(item)
    assert progress_before_result
    assert FeedbackKind.ESTIMATE not in kinds
    assert kinds.count(FeedbackKind.RESULT) == 1
    assert kinds[-1] is FeedbackKind.RESULT
    # 中间 tool_execution_end 只记 progress，不提前交 RESULT。
    assert kinds[0] is FeedbackKind.PROGRESS
    assert kinds[1] is FeedbackKind.PROGRESS
    assert results[-1].result is not None
    assert results[-1].result.summary == "完成"
    prompt = engine._build_prompt(
        ToolRequest(
            need="x",
            command="echo",
            expected_result="一句",
            field_ref={"zone_kind": "wood"},
        )
    )
    assert "echo" in prompt
    assert "一句" in prompt
    assert "wood" in prompt


def test_pi_engine_one_result_prefers_shell_output() -> None:
    """读文件 / ls 不当终态；整轮只交一条 RESULT，且优先脚本输出。"""
    from jshi.tool.pi_engine import PiEngine

    lines = (
        json.dumps(
            {
                "type": "tool_execution_end",
                "toolName": "read",
                "isError": False,
                "result": {
                    "content": [{"type": "text", "text": "# Usage: weather.ps1 ..."}]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        json.dumps(
            {
                "type": "tool_execution_end",
                "toolName": "bash",
                "isError": False,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Hangzhou 2026-09-14: Overcast, 22~27C\r\n",
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "明天阴天，22~27°C。"}],
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        json.dumps({"type": "agent_settled"}, ensure_ascii=False) + "\n",
    )
    engine = PiEngine()
    engine._spawn = lambda: _FakePopen(lines)
    items = list(engine.iter_execute(ToolRequest(need="查天气", command="skill:get-weather")))
    results = [item for item in items if item.kind is FeedbackKind.RESULT]
    progresses = [item for item in items if item.kind is FeedbackKind.PROGRESS]
    assert len(progresses) == 2
    assert len(results) == 1
    assert results[0].result is not None
    assert results[0].result.status is ToolStatus.OK
    assert "Hangzhou 2026-09-14" in (results[0].result.summary or "")


def test_pi_engine_timeout_writes_failed_result() -> None:
    from jshi.tool.pi_engine import PiEngine

    engine = PiEngine(timeout_s=0.2)
    engine._spawn = lambda: _FakePopen(hang=True)
    items = list(engine.iter_execute(ToolRequest(need="x", command="echo")))
    assert items
    assert items[-1].kind is FeedbackKind.RESULT
    assert items[-1].result is not None
    assert items[-1].result.status is ToolStatus.FAILED
    assert "超时" in items[-1].result.error


def test_resolve_timeout_s_tiers() -> None:
    from jshi.tool.service import (
        TIMEOUT_CREATE_S,
        TIMEOUT_MAX_S,
        TIMEOUT_USE_S,
        resolve_timeout_s,
    )

    assert resolve_timeout_s("use") == TIMEOUT_USE_S
    assert resolve_timeout_s("create") == TIMEOUT_CREATE_S
    assert resolve_timeout_s("use", {"time_est_ms": 10_000}) == TIMEOUT_USE_S
    # 估价放大超过用档底线
    assert resolve_timeout_s("use", {"time_est_ms": 100_000}) == 300.0
    assert resolve_timeout_s("create", {"time_est_ms": 1_000_000}) == TIMEOUT_MAX_S


def test_pi_engine_honours_request_timeout_meta() -> None:
    from jshi.tool.pi_engine import PiEngine

    engine = PiEngine(timeout_s=30)
    engine._spawn = lambda: _FakePopen(hang=True)
    started = time.monotonic()
    items = list(
        engine.iter_execute(
            ToolRequest(need="x", command="echo", meta={"timeout_s": 0.25})
        )
    )
    elapsed = time.monotonic() - started
    assert elapsed < 5
    assert items[-1].result is not None
    assert items[-1].result.status is ToolStatus.FAILED
    assert "超时" in items[-1].result.error


def test_plan_payload_includes_in_flight(tmp_path: Path) -> None:
    from jshi.tool.intake import IntakeRecord
    from jshi.tool.plan import PlanFailure

    hang = HangStore(tmp_path / "hang.jsonl")
    hang.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="查杭州天气",
        command="skill:get-weather",
        kind="use",
    )
    captured: dict = {}

    class _CapturePlan(ToolPlanSkill):
        def plan_intake(self, intake, catalog, engine_tools=(), scene="", in_flight=()):
            captured["in_flight"] = list(in_flight)
            return PlanFailure("停")

    planner = SkillPlanner(
        _CapturePlan(_JsonPort({"ok": False, "error": "停"})),
        catalog=load_catalog(),
        engine_tools=(
            {
                "name": "skill:get-weather",
                "description": "按地点查询近日天气，返回概况与温度。",
            },
        ),
        hang_store=hang,
    )
    result = planner.plan(
        IntakeRecord(
            intake_id="i1",
            subject_id="stone",
            object_id="OBJ-A",
            need="再查杭州",
        )
    )
    assert isinstance(result, PlanFailure)
    assert captured["in_flight"]
    item = captured["in_flight"][0]
    assert item["phase"] == "use"
    assert item["tool_name"] == "skill:get-weather"
    assert item["purpose"] == "按地点查询近日天气，返回概况与温度。"
    assert item["need"] == "查杭州天气"
    assert item["progress"] == "running"
    assert "summary" not in item
    assert "command" not in item


def test_plan_in_flight_create_uses_tool_intent(tmp_path: Path) -> None:
    from jshi.tool.intake import IntakeRecord
    from jshi.tool.plan import PlanFailure

    hang = HangStore(tmp_path / "hang.jsonl")
    hang.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="需要一个能查人民币兑美元汇率的工具",
        command="exchange-rate",
        kind="create",
        meta={
            "tool_intent": "按币种对查询即期汇率，返回数值。",
        },
    )
    captured: dict = {}

    class _CapturePlan(ToolPlanSkill):
        def plan_intake(self, intake, catalog, engine_tools=(), scene="", in_flight=()):
            captured["in_flight"] = list(in_flight)
            return PlanFailure("停")

    planner = SkillPlanner(
        _CapturePlan(_JsonPort({"ok": False, "error": "停"})),
        catalog=load_catalog(),
        hang_store=hang,
    )
    planner.plan(
        IntakeRecord(
            intake_id="i1",
            subject_id="stone",
            object_id="OBJ-A",
            need="查日元汇率",
        )
    )
    item = captured["in_flight"][0]
    assert item["phase"] == "create"
    assert item["tool_name"] == "exchange-rate"
    assert item["purpose"] == "按币种对查询即期汇率，返回数值。"


def test_launch_persists_create_intent_for_in_flight(tmp_path: Path) -> None:
    from jshi.tool.contract import CreateToolSpec

    hang = HangStore(tmp_path / "hang.jsonl")
    runner = ToolRunner(ToolModule(StubEngine()), hang)
    service = ToolService(
        hang,
        runner,
        planner=RulePlanner(),
        intake_path=tmp_path / "tool.jsonl",
    )
    intake = service.intake_store.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="造一个汇率工具",
    )
    request = ToolRequest(
        need="造一个汇率工具",
        ask=AskMode.CREATE_TOOL,
        create=CreateToolSpec(
            tool_name="exchange-rate",
            tool_intent="按币种对查询即期汇率，返回数值。",
            expected_output="汇率数字",
        ),
    )
    task_id = service._launch(intake, request)
    record = hang.get(task_id)
    assert record is not None
    assert record.kind == "create"
    assert record.command == "exchange-rate"
    assert record.meta.get("tool_intent") == "按币种对查询即期汇率，返回数值。"
    service.drain_for_tests()


def test_cancel_stops_pi_session(tmp_path: Path) -> None:
    from jshi.tool.pi_engine import PiEngine

    engine = PiEngine(timeout_s=30)
    holder: dict[str, _FakePopen] = {}

    def spawn() -> _FakePopen:
        proc = _FakePopen(hang=True)
        holder["p"] = proc
        return proc

    engine._spawn = spawn
    store = HangStore(tmp_path / "hang.jsonl")
    runner = ToolRunner(ToolModule(engine), store)
    record = store.create(subject_id="stone", object_id="OBJ-A", need="查一下")
    runner.start(
        ToolRequest(request_id=record.request_id, need="查一下", command="echo"),
        record.task_id,
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and "p" not in holder:
        time.sleep(0.02)
    assert "p" in holder
    runner.cancel(record.task_id)
    runner.drain_for_tests()
    assert holder["p"].returncode is not None
    done = store.get(record.task_id)
    assert done is not None
    terminal = next(
        item.result
        for item in reversed(done.feedback)
        if item.kind is FeedbackKind.RESULT and item.result is not None
    )
    assert terminal.status is ToolStatus.ABORTED
    assert "取消" in terminal.error


def test_pi_list_commands_timeout_returns_empty() -> None:
    from jshi.tool.pi_engine import PiEngine

    engine = PiEngine(timeout_s=120, list_timeout_s=0.2)
    engine._spawn = lambda: _FakePopen(hang=True)
    started = time.monotonic()
    assert engine.list_commands() == ()
    assert time.monotonic() - started < 3


def test_planner_lists_engine_tools_lazily() -> None:
    class _CountEngine(StubEngine):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def list_commands(self):
            self.calls += 1
            return super().list_commands()

    engine = _CountEngine()
    planner = SkillPlanner(
        ToolPlanSkill(_JsonPort({"ok": True, "template": "echo", "command": "echo"})),
        catalog=load_catalog(),
        engine=engine,
    )
    assert engine.calls == 0
    from jshi.tool.intake import IntakeRecord

    planned = planner.plan(
        IntakeRecord(
            intake_id="in-1",
            subject_id="stone",
            object_id="OBJ-A",
            need="查一下天气",
        )
    )
    from jshi.tool.plan import PlanFailure

    assert not isinstance(planned, PlanFailure)
    assert engine.calls == 1
    assert planned.command == "echo"


def _workspace_tmp(name: str) -> Path:
    root = Path(__file__).resolve().parents[1] / ".tmp" / "tool-ws" / name
    root.mkdir(parents=True, exist_ok=True)
    path = root / str(time.time_ns())
    path.mkdir()
    return path


def test_service_wrap_contract_without_tmp_path() -> None:
    """不走 pytest tmp_path，避免本机 basetemp scandir 把验收标成 ERROR。"""
    folder = _workspace_tmp("wrap-contract")
    store = HangStore(folder / "hang.jsonl")
    engine = _StreamEngine()
    runner = ToolRunner(ToolModule(engine), store)
    wrap = ToolWrapSkill(_JsonPort({"visible": True, "summary": "啊" * 200}))
    service = ToolService(
        store,
        runner,
        wrap_skill=wrap,
        intake_path=folder / "tool.jsonl",
    )
    service.intake(subject_id="stone", object_id="OBJ-A", need="查一下天气")
    service.drain_planning_for_tests()
    assert engine.after_progress.wait(timeout=5)
    deadline = time.monotonic() + 3
    mid = None
    while time.monotonic() < deadline:
        hangs = store.list_for("stone", "OBJ-A")
        if hangs and hangs[0].visible:
            mid = hangs[0]
            break
        time.sleep(0.05)
    assert mid is not None
    kinds = [item.kind for item in mid.feedback]
    assert kinds.count(FeedbackKind.ESTIMATE) == 1
    assert mid.status == "open"
    engine.go_result.set()
    service.drain_for_tests()
    done = store.get(mid.task_id)
    assert done is not None
    assert len(done.summary) == NOTE_MAX_CHARS
    assert done.status == "notified"


# --------------------------------------------------------------------------- #
# 送入主流程：标记已见、不再走 list_visible、回应回写记挂（不再写片场工具块）
# --------------------------------------------------------------------------- #


def test_scene_block_form_is_knowledge_not_speech() -> None:
    from jshi.tool.service import (
        SCENE_BLOCK_CHARS,
        SCENE_BLOCK_PREFIX,
        format_scene_block,
    )

    block = format_scene_block("我已经查了明天杭州的天气，是多云转晴")
    assert block.startswith(SCENE_BLOCK_PREFIX)
    assert "我说" not in block
    assert format_scene_block("") == ""
    # 重复修饰不叠加前缀
    assert format_scene_block(block).count(SCENE_BLOCK_PREFIX) == 1
    # 单条上限
    long_block = format_scene_block("啊" * 400)
    assert len(long_block) == SCENE_BLOCK_CHARS


def test_delivered_feedback_leaves_tool_input_and_keeps_ledger() -> None:
    folder = _workspace_tmp("deliver")
    store = HangStore(folder / "hang.jsonl")
    service = ToolService(
        store,
        ToolRunner(ToolModule(StubEngine()), store),
        intake_path=folder / "tool.jsonl",
    )
    record = store.create(subject_id="stone", object_id="OBJ-A", need="查一下天气")
    store.set_wrap(record.task_id, visible=True, summary="明天杭州多云转晴", terminal=True)
    assert service.list_visible("stone", "OBJ-A")

    blocks, delivered = service.deliver_to_scene("stone", "OBJ-A")
    assert blocks == ()
    assert delivered == (record.task_id,)
    assert service.list_visible("stone", "OBJ-A") == ()

    done = store.get(record.task_id)
    assert done is not None
    assert done.delivered_at is not None
    assert done.visible is True
    assert service.deliver_to_scene("stone", "OBJ-A") == ((), ())
    assert service.deliver_to_scene("stone", "OBJ-B") == ((), ())


def test_tool_response_written_back_to_hang() -> None:
    folder = _workspace_tmp("writeback")
    store = HangStore(folder / "hang.jsonl")
    service = ToolService(
        store,
        ToolRunner(ToolModule(StubEngine()), store),
        intake_path=folder / "tool.jsonl",
    )
    record = store.create(subject_id="stone", object_id="OBJ-A", need="查一下天气")
    store.set_wrap(record.task_id, visible=True, summary="明天多云", terminal=True)
    service.write_back_response(
        (record.task_id,),
        {"mode": "respond", "reply": "已经查到了，明天多云", "action": "", "text": "已经查到了，明天多云"},
    )
    service.write_back_response(
        (record.task_id,),
        {"mode": "think", "reply": "", "action": "", "text": ""},
    )
    done = store.get(record.task_id)
    assert done is not None
    assert len(done.responses) == 2            # 只增
    assert done.responses[0]["reply"] == "已经查到了，明天多云"
    assert done.responses[1]["mode"] == "think"  # 没开口也留轮次


def test_append_only_roundtrip_keeps_delivery_and_responses() -> None:
    folder = _workspace_tmp("roundtrip")
    path = folder / "hang.jsonl"
    store = HangStore(path)
    record = store.create(subject_id="stone", object_id="OBJ-A", need="查一下")
    store.set_wrap(record.task_id, visible=True, summary="一句", terminal=True)
    store.set_delivered(record.task_id, block="（我知道）一句")
    store.set_response(record.task_id, {"mode": "respond", "reply": "好"})

    loaded = HangStore(path)
    got = loaded.get(record.task_id)
    assert got is not None
    assert got.delivered_at is not None
    assert got.delivered_block == "（我知道）一句"
    assert got.responses[0]["reply"] == "好"


def test_delivery_marks_main_seen_without_scene_block(tmp_path: Path) -> None:
    """可见句送入主流程后标记已见；不再写成片场「（我知道）」块。"""
    process, _repository, store, service = _runtime(
        tmp_path, tool_intent=ToolUseIntent(need="查一下天气")
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    object_id = result.speaker.object_id
    visible = service.list_visible("stone", object_id)
    assert visible
    task_id = visible[0].id

    process._turn_tool_ids = (task_id,)
    blocks, delivered = process._tool_scene_blocks("stone", object_id)
    assert delivered == (task_id,)
    assert blocks == ()

    assert service.list_visible("stone", object_id) == ()
    done = store.get(task_id)
    assert done is not None and done.delivered_at is not None
    zone = process.zone_store.get("stone")
    assert not any(text.startswith("（我知道）") for text in zone)


def _persona_runtime(tmp_path: Path, *, summary: str = "明天杭州多云转晴"):
    """人格片场（苏西坡）+ 已就绪片场，用来验交付与回写。"""
    from jshi.style import StylePackStore

    process, repository, store, service = _runtime(
        tmp_path, tool_intent=ToolUseIntent(need="查一下天气")
    )
    packs = StylePackStore(tmp_path / "style_pack.json")
    packs.set("stone", "suxipo")
    process.style_packs = packs
    process.zone_store.save("stone", ["场面开头"])
    record = store.create(subject_id="stone", object_id="OBJ-USER", need="查杭州天气")
    store.set_wrap(record.task_id, visible=True, summary=summary, terminal=True)
    return process, repository, store, service, record


def test_persona_round_marks_tool_seen_without_scene_block(tmp_path: Path) -> None:
    """真跑一轮人格：工具进【工具相关】并标记已见；不写「（我知道）」进片场。"""
    process, _repository, store, service, record = _persona_runtime(tmp_path)
    result = process.experience("stone", "查到了吗", object_ref="user")
    service.drain_for_tests()

    zone = process.zone_store.get("stone")
    assert not any(text.startswith("（我知道）") for text in zone)

    done = store.get(record.task_id)
    assert done is not None
    assert done.delivered_at is not None
    visible_ids = {item.id for item in service.list_visible("stone", "OBJ-USER")}
    assert record.task_id not in visible_ids
    assert result.activity.status == ActivityStatus.COMPLETED
    assert done.responses, "05 回应应回写进该工具记挂"
    assert done.responses[0]["reply"] == result.action_text
    related = service.format_tool_related_block("stone", "OBJ-USER")
    assert "【工具相关】" in related
    assert "多云转晴" in related or "最终的结果" in related


def test_cli_turn_view_reads_main_flow_tool_intent(tmp_path: Path) -> None:
    """主流程这一拍给了 200 什么：05 指示落库 + 200 侧可见/已交付/已回写。"""
    import argparse
    import contextlib
    import io

    from jshi.app import cli as cli_module
    from jshi.tool.view import format_turn_view

    process, repository, _store, service = _runtime(
        tmp_path, tool_intent=ToolUseIntent(need="查一下天气")
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    object_id = result.speaker.object_id

    # 1) 05 指示落库
    marked = [
        item
        for item in repository.list_history("stone", HistoryKind.FACT)
        if item.event_type == "activity_response_marked"
    ]
    assert marked, "主流程应把 05 的工具指示落成历史记录"
    assert marked[-1].content["use_tool"] is True
    assert marked[-1].content["need"] == "查一下天气"
    assert marked[-1].content["reply"] == "我去查一下"

    # 2) CLI 版视图能看到这段 + 200 侧状态
    history = [(item.event_type, dict(item.content or {})) for item in marked]
    text = format_turn_view(service, history, subject_id="stone", object_id=object_id)
    assert "use_tool=true" in text
    assert "查一下天气" in text
    assert "【200 侧（按当前账本算）】" in text

    # 3) 走命令行入口也不炸（--turn）
    args = argparse.Namespace(
        data_dir=tmp_path,
        subject_id="stone",
        object_id=object_id,
        speaker="user",
        id="",
        list=False,
        raw=False,
        turn=True,
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cli_module._run_tool_log(args)
    out = buffer.getvalue()
    assert "use_tool=true" in out


def test_persona_write_back_keeps_turn_when_silent(tmp_path: Path) -> None:
    """没开口（think）也留一条轮次。"""
    process, _repository, store, service, record = _persona_runtime(tmp_path)
    process.cognition = _PlanModel(
        ResponsePlan(mode="think", reason="先不说话", items=())
    )
    process.experience("stone", "查到了吗", object_ref="user")
    service.drain_for_tests()
    done = store.get(record.task_id)
    assert done is not None
    assert done.responses
    assert done.responses[0]["mode"] == "think"
    assert done.responses[0]["reply"] == ""


def test_plan_failure_sentence_also_marks_main_seen(tmp_path: Path) -> None:
    """策划失败句送入主流程后标记已见，不再以「新的信息」反复出现。"""
    process, _repository, _store, service = _runtime(
        tmp_path, tool_intent=ToolUseIntent(need="x")
    )
    result = process.experience("stone", "查一下", object_ref="user")
    service.drain_for_tests()
    object_id = result.speaker.object_id

    before = service.list_visible("stone", object_id)
    assert len(before) == 1 and before[0].kind == "plan_failed"

    blocks, delivered = service.deliver_to_scene("stone", object_id)
    assert blocks == ()
    assert delivered == (before[0].id,)
    assert service.list_visible("stone", object_id) == ()
    record = service.intake_store.get(before[0].id)
    assert record is not None
    assert record.delivered_at is not None
    service.revoke_delivery(before[0].id)
    assert service.list_visible("stone", object_id)


def test_persona_round_marks_plan_failure_seen(tmp_path: Path) -> None:
    """人格真跑一轮：上一拍策划失败进入【工具相关】并标记已见；不写进片场。"""
    from jshi.style import StylePackStore

    process, _repository, _store, service = _runtime(tmp_path, tool_intent=None)
    packs = StylePackStore(tmp_path / "style_pack.json")
    packs.set("stone", "suxipo")
    process.style_packs = packs
    process.zone_store.save("stone", ["场面开头"])

    service.intake(subject_id="stone", object_id="OBJ-USER", need="x")
    service.drain_planning_for_tests()
    assert [i.kind for i in service.list_visible("stone", "OBJ-USER")] == ["plan_failed"]

    process.experience("stone", "还在吗", object_ref="user")
    service.drain_for_tests()
    zone = process.zone_store.get("stone")
    assert not any(text.startswith("（我知道）") for text in zone)
    assert service.list_visible("stone", "OBJ-USER") == ()
    related = service.format_tool_related_block("stone", "OBJ-USER")
    assert "【工具相关】" in related
    assert "失败" in related


def test_plan_instruction_forbids_internal_names_in_failure_line() -> None:
    """失败句给人看，不许写字段名、要有主语。"""
    text = ToolPlanSkill.instruction
    assert "不要写字段名" in text
    assert "engine_tools" in text          # 只作为「不许写」的举例
    assert "我手上现在没有能查天气的工具" in text
    assert "scene" in text


def test_plan_skill_reads_scene_not_into_intake(tmp_path: Path) -> None:
    from jshi.tool.intake import IntakeRecord

    marker = "MARKER-YUHANG-SCENE"
    port = _JsonPort({"ok": True, "template": "echo", "command": "echo"})
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=({"name": "echo", "description": "回显"},),
        scene_loader=lambda _intake: marker,
    )
    planned = planner.plan(
        IntakeRecord(
            intake_id="in-1",
            subject_id="stone",
            object_id="OBJ-A",
            need="查一下天气",
            field_ref={"zone_kind": "wood", "zone_rev": "1"},
        )
    )
    from jshi.tool.plan import PlanFailure

    assert not isinstance(planned, PlanFailure)
    assert port.last_request is not None
    payload = json.loads(port.last_request.input_text)
    assert payload["scene"] == marker
    assert payload["field_ref"]["zone_kind"] == "wood"
    assert "rewritten_context" not in payload["field_ref"]


def test_plan_skill_scene_loader_failure_is_empty() -> None:
    from jshi.tool.intake import IntakeRecord
    from jshi.tool.plan import PlanFailure

    def _boom(_intake):
        raise RuntimeError("boom")

    port = _JsonPort({"ok": True, "template": "echo", "command": "echo"})
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=({"name": "echo", "description": "回显"},),
        scene_loader=_boom,
    )
    planned = planner.plan(
        IntakeRecord(
            intake_id="in-1",
            subject_id="stone",
            object_id="OBJ-A",
            need="查一下天气",
        )
    )
    assert not isinstance(planned, PlanFailure)
    assert port.last_request is not None
    payload = json.loads(port.last_request.input_text)
    assert payload["scene"] == ""


def test_process_plan_reads_wood_zone_not_copied_to_jsonl(tmp_path: Path) -> None:
    marker = "MARKER-WOOD-SCENE"
    port = _JsonPort({"ok": True, "template": "echo", "command": "echo"})
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=({"name": "echo", "description": "回显"},),
    )
    process, _repository, _hang, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=planner,
    )
    process.activity_ledger.save_rewritten_context(
        "stone", marker, speaker_object_id="OBJ-USER"
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    assert port.last_request is not None
    payload = json.loads(port.last_request.input_text)
    assert payload["scene"] == marker
    ledger = (tmp_path / "tool.jsonl").read_text(encoding="utf-8")
    assert marker not in ledger
    record = service.intake_store.list_for("stone", result.speaker.object_id)[0]
    assert "rewritten_context" not in record.field_ref
    assert record.field_ref.get("zone_kind") == "wood"


def test_process_plan_reads_persona_zone(tmp_path: Path) -> None:
    from jshi.style import StylePackStore

    marker = "MARKER-PERSONA-SCENE"
    port = _JsonPort({"ok": True, "template": "echo", "command": "echo"})
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=({"name": "echo", "description": "回显"},),
    )
    process, _repository, _hang, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=planner,
    )
    packs = StylePackStore(tmp_path / "style_pack.json")
    packs.set("stone", "suxipo")
    process.style_packs = packs
    process.zone_store.save("stone", [marker])
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    assert port.last_request is not None
    payload = json.loads(port.last_request.input_text)
    assert marker in payload["scene"]
    record = service.intake_store.list_for("stone", result.speaker.object_id)[0]
    assert record.field_ref.get("zone_kind") == "persona"


def test_revoke_delivery_keeps_result_visible() -> None:
    """撤销「已送入主流程」后，可见句再次出现。"""
    folder = _workspace_tmp("revoke")
    store = HangStore(folder / "hang.jsonl")
    service = ToolService(
        store,
        ToolRunner(ToolModule(StubEngine()), store),
        intake_path=folder / "tool.jsonl",
    )
    record = store.create(subject_id="stone", object_id="OBJ-A", need="查一下")
    store.set_wrap(record.task_id, visible=True, summary="一句", terminal=True)
    assert service.deliver_to_scene("stone", "OBJ-A")[1]
    assert service.list_visible("stone", "OBJ-A") == ()
    service.revoke_delivery(record.task_id)
    assert service.list_visible("stone", "OBJ-A")
    done = store.get(record.task_id)
    assert done is not None and done.delivered_at is None


def test_zone_budget_note_reports_tool_block_budget() -> None:
    from jshi.style import format_zone_budget_note

    note = format_zone_budget_note(100, 2000, tool_chars=30, tool_cap=720)
    assert "【工具块】" in note
    assert "30" in note and "720" in note
    assert "【工具相关】" in note
    plain = format_zone_budget_note(100, 2000)
    assert "【工具块】" not in plain


# --------------------------------------------------------------------------- #
# 多工具计划：落账与调度语义（见 方案(coding)/200-计划落账与调度语义.md）
# --------------------------------------------------------------------------- #


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """等一个条件成立（计划调度在后台线程，断言前得先等它跑到）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _StatusEngine:
    """按 need 返回指定终态；记录执行过的请求，供「后续没启动」的断言用。"""

    name = "status"

    def __init__(self, statuses: dict[str, ToolStatus] | None = None) -> None:
        self._statuses = dict(statuses or {})
        self.seen: list[str] = []

    def list_templates(self) -> tuple[str, ...]:
        return ("echo",)

    def list_commands(self):
        return ({"name": "echo", "description": "回显，仅测试"},)

    def iter_execute(self, request: ToolRequest, cancel=None):
        del cancel
        key = request.need or ""
        self.seen.append(key)
        yield ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.PROGRESS,
            progress=ToolProgress(stage="running", partial="半条"),
        )
        yield ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.RESULT,
            result=ToolResult(
                status=self._statuses.get(key, ToolStatus.OK),
                summary=f"{key} 结果",
                time_ms=1,
            ),
        )

    def execute(self, request: ToolRequest):
        return tuple(self.iter_execute(request))


class _DepPlanner:
    def __init__(self, plan: ToolPlan) -> None:
        self._plan = plan

    def plan(self, intake):
        del intake
        return self._plan


def _dep_plan(mode: str = "sequential") -> ToolPlan:
    """a → b 的两步计划。"""
    return ToolPlan(
        plan_id="plan-dep",
        need="查天气",
        mode=mode,
        steps=(
            ToolStep(
                step_id="a",
                request=ToolRequest(
                    need="a",
                    template="generic",
                    command="echo",
                    params={},
                    ask=AskMode.EXECUTE,
                ),
            ),
            ToolStep(
                step_id="b",
                request=ToolRequest(
                    need="b",
                    template="generic",
                    command="echo",
                    params={},
                    ask=AskMode.EXECUTE,
                ),
                depends_on=("a",),
            ),
        ),
    )


def test_failed_step_freezes_dependents(tmp_path: Path) -> None:
    """前序失败 → 依赖它的后续不启动，计划标 blocked。"""
    engine = _StatusEngine({"a": ToolStatus.FAILED})
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=_DepPlanner(_dep_plan()),
        engine=engine,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    assert engine.seen == ["a"]  # b 从没启动
    assert len(store.list_for("stone", result.speaker.object_id)) == 1
    summary = service.plan_summary("plan-dep")
    assert summary is not None
    assert summary["status"] == "blocked"
    assert "a" in summary["note"]


def test_partial_step_also_freezes_dependents(tmp_path: Path) -> None:
    """拍板 1 的边界：部分完成不算做成，同样冻结。"""
    engine = _StatusEngine({"a": ToolStatus.PARTIAL})
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=_DepPlanner(_dep_plan()),
        engine=engine,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    assert engine.seen == ["a"]
    assert len(store.list_for("stone", result.speaker.object_id)) == 1
    assert service.plan_summary("plan-dep")["status"] == "blocked"


def test_cancel_plan_does_not_unlock_dependents(tmp_path: Path) -> None:
    """取消已启动的前序，绝不能反过来解锁后续；取消的这一次不计入实测统计。"""

    class _CancelEngine(_StatusEngine):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()

        def iter_execute(self, request: ToolRequest, cancel=None):
            self.seen.append(request.need or "")
            self.started.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    break
                time.sleep(0.005)
            yield ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(status=ToolStatus.ABORTED, error="已取消"),
            )

    engine = _CancelEngine()
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=_DepPlanner(_dep_plan()),
        engine=engine,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_planning_for_tests()
    assert _wait_until(engine.started.is_set)
    assert service.cancel_plan("plan-dep") is True
    service.drain_for_tests()
    assert engine.seen == ["a"]  # 取消没有引出 b
    assert len(store.list_for("stone", result.speaker.object_id)) == 1
    assert service.plan_summary("plan-dep")["status"] == "cancelled"
    assert service.metrics.stats("echo").runs == 0  # 取消不污染实测均值


def test_sequential_plan_auto_chains_missing_deps(tmp_path: Path) -> None:
    """mode=sequential 且步骤没写 depends_on → 程序按声明顺序补链。"""

    class _GatedEngine(_StatusEngine):
        def __init__(self) -> None:
            super().__init__()
            self.release_a = threading.Event()
            self.b_started = threading.Event()

        def iter_execute(self, request: ToolRequest, cancel=None):
            del cancel
            key = request.need or ""
            self.seen.append(key)
            if key == "a":
                self.release_a.wait(timeout=5)
            else:
                self.b_started.set()
            yield ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(status=ToolStatus.OK, summary=f"{key} 结果"),
            )

    plan = ToolPlan(
        plan_id="plan-seq",
        need="查天气",
        mode="sequential",
        steps=(
            ToolStep(
                step_id="a",
                request=ToolRequest(need="a", command="echo", ask=AskMode.EXECUTE),
            ),
            # 故意不写 depends_on：靠 normalize_plan 补成 a → b
            ToolStep(
                step_id="b",
                request=ToolRequest(need="b", command="echo", ask=AskMode.EXECUTE),
            ),
        ),
    )
    engine = _GatedEngine()
    process, _repository, _store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=_DepPlanner(plan),
        engine=engine,
    )
    process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_planning_for_tests()
    assert _wait_until(lambda: engine.seen == ["a"])
    assert engine.b_started.is_set() is False  # a 还在跑，b 不得启动
    engine.release_a.set()
    service.drain_for_tests()
    assert engine.seen == ["a", "b"]
    assert service.plan_summary("plan-seq")["status"] == "done"


def test_cyclic_plan_is_rejected(tmp_path: Path) -> None:
    """依赖成环的计划永远等不到就绪，必须当场拒掉，不能建出永远 open 的账。"""
    plan = ToolPlan(
        plan_id="plan-cycle",
        need="查天气",
        mode="parallel",
        steps=(
            ToolStep(
                step_id="a",
                request=ToolRequest(need="a", command="echo", ask=AskMode.EXECUTE),
                depends_on=("b",),
            ),
            ToolStep(
                step_id="b",
                request=ToolRequest(need="b", command="echo", ask=AskMode.EXECUTE),
                depends_on=("a",),
            ),
        ),
    )
    engine = _StatusEngine()
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=_DepPlanner(plan),
        engine=engine,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    assert store.list_for("stone", result.speaker.object_id) == ()
    assert engine.seen == []
    visible = service.list_visible("stone", result.speaker.object_id)
    assert visible
    assert "排不出顺序" in visible[0].summary


def test_propose_only_step_is_rejected(tmp_path: Path) -> None:
    """计划里的步骤不能是「先报价、等人批准」。

    `_launch` 见到 propose_only 只会停在提案、不启动 runner，于是这一步永远没有终态，
    整条计划静默卡在 running，后续步骤永远不启动——而计划中途没人能批准。
    """
    plan = ToolPlan(
        plan_id="plan-prop",
        need="查天气",
        mode="parallel",
        steps=(
            ToolStep(
                step_id="a",
                request=ToolRequest(need="a", command="echo", ask=AskMode.PROPOSE_ONLY),
            ),
        ),
    )
    engine = _StatusEngine()
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=_DepPlanner(plan),
        engine=engine,
    )
    result = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    assert store.list_for("stone", result.speaker.object_id) == ()
    assert engine.seen == []
    visible = service.list_visible("stone", result.speaker.object_id)
    assert visible
    assert "先问过再跑" in visible[0].summary


def test_wrap_payload_carries_ideal_flag(tmp_path: Path) -> None:
    """包装要看得见「这一步有没有真实产出」，否则会把回显写成成功。"""

    class _RecordingPort:
        name = "record"

        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def generate(self, request: ModelRequest) -> ModelResponse:
            self.payloads.append(json.loads(request.input_text))
            text = '{"visible": true, "summary": "一句"}'
            return ModelResponse(
                model=self.name,
                response_plan=ResponsePlan(
                    mode="respond",
                    items=(ResponseItem(channel="verbal", text=text),),
                ),
                raw_text=text,
            )

    port = _RecordingPort()
    process, _repository, _store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        engine=StubEngine(),                      # 占位引擎：ideal=False
        wrap_skill=ToolWrapSkill(port),
    )
    process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    result_payloads = [p for p in port.payloads if "ideal" in p]
    assert result_payloads
    payload = result_payloads[-1]
    assert payload["ideal"] is False
    assert "占位" in payload["ideal_note"]


def test_created_tool_name_is_normalized_before_ledger() -> None:
    """造工具的名字要在落账前规范化。

    2026-09-17 实测：模型给 ``weather_forecast``，Pi 那边造出 ``weather-forecast``，
    账本按原样记了带下划线的那个，于是账本 / 引擎技能名 / tool_metrics 的 key 三处对不上。
    """
    from jshi.tool.contract import normalize_tool_name

    spec = ToolPlanSkill._parse_create(
        {"create_tool": {"tool_name": "weather_forecast", "tool_intent": "查天气"}},
        AskMode.CREATE_TOOL,
    )
    assert spec is not None
    assert spec.tool_name == "weather-forecast"
    assert spec.tool_name == normalize_tool_name("weather_forecast")


def test_pi_prompt_uses_official_skill_command_form() -> None:
    """技能按 Pi 官方形式调用：``/skill:<name> <args>``。

    官方文档：技能注册成 /skill:name 命令，「Load and execute the skill」，
    后面的参数作为 ``User: <args>`` 追加到技能正文后。

    2026-09-13 实测：写成「【工具】skill:get-weather」时 Pi 只读 SKILL.md、
    把文件内容当结果交回来，从不执行；换成斜杠形式一次跑通。
    """
    from jshi.tool.pi_engine import PiEngine

    prompt = PiEngine()._build_prompt(
        ToolRequest(
            need="查明天杭州天气",
            command="skill:get-weather",
            params={"city": "杭州"},
            expected_result="杭州明天的天气",
        )
    )
    assert prompt == "/skill:get-weather 查明天杭州天气"


def test_pi_prompt_keeps_plain_form_for_non_skill_commands() -> None:
    """非技能命令走原来的任务式 prompt，别被技能形式带偏。"""
    from jshi.tool.pi_engine import PiEngine

    prompt = PiEngine()._build_prompt(
        ToolRequest(need="跑一下", command="llama", params={"x": 1})
    )
    assert prompt.startswith("请完成下面这一次工具使用。")
    assert "【工具】" in prompt


def test_wrap_payload_carries_plan_identity(tmp_path: Path) -> None:
    """包装要看得见「这是计划里的第几步」，否则只会把每一步都写成整件事。"""

    class _RecordingPort:
        name = "record"

        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def generate(self, request: ModelRequest) -> ModelResponse:
            self.payloads.append(json.loads(request.input_text))
            text = '{"visible": true, "summary": "一句"}'
            return ModelResponse(
                model=self.name,
                response_plan=ResponsePlan(
                    mode="respond",
                    items=(ResponseItem(channel="verbal", text=text),),
                ),
                raw_text=text,
            )

    port = _RecordingPort()
    engine = _StatusEngine()
    process, _repository, _store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
        planner=_DepPlanner(_dep_plan()),
        engine=engine,
        wrap_skill=ToolWrapSkill(port),
    )
    process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    step_payloads = [item for item in port.payloads if "step_id" in item]
    assert {item["step_id"] for item in step_payloads} == {"a", "b"}
    for item in step_payloads:
        assert item["plan_id"] == "plan-dep"
        assert item["plan_total"] == 2
    by_step = {item["step_id"]: item for item in step_payloads}
    assert by_step["a"]["plan_index"] == 1
    assert by_step["b"]["plan_index"] == 2
    assert by_step["b"]["is_plan_final"] is True


def test_list_hot_state_running_and_recent_done(tmp_path: Path) -> None:
    from dataclasses import replace
    from datetime import timedelta

    from jshi.tool.contract import utc_now
    from jshi.tool.service import format_tool_hot_state

    hang = HangStore(tmp_path / "hang.jsonl")
    service = ToolService(
        hang,
        ToolRunner(ToolModule(StubEngine()), hang),
        intake_path=tmp_path / "tool.jsonl",
    )
    open_rec = hang.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="查杭州天气",
        command="skill:get-weather",
        kind="use",
    )
    hot = service.list_hot_state("stone", "OBJ-A")
    assert len(hot) == 1
    assert hot[0].phase == "running"
    assert "进行中" in format_tool_hot_state(hot)

    hang.set_wrap(
        open_rec.task_id,
        visible=True,
        summary="杭州阴天 22~27°C",
        terminal=True,
    )
    hot = service.list_hot_state("stone", "OBJ-A")
    assert len(hot) == 1
    assert hot[0].phase == "done"
    assert "已完成" in format_tool_hot_state(hot)
    # 已交付也不影响热状态（05 仍要看见近时终态）
    hang.set_delivered(open_rec.task_id, block="（我知道）杭州阴天")
    assert service.list_visible("stone", "OBJ-A") == ()
    hot = service.list_hot_state("stone", "OBJ-A")
    assert len(hot) == 1
    assert hot[0].phase == "done"

    done = hang.get(open_rec.task_id)
    assert done is not None
    hang._records[open_rec.task_id] = replace(
        done, updated_at=utc_now() - timedelta(hours=2)
    )
    assert service.list_hot_state("stone", "OBJ-A") == ()
    assert format_tool_hot_state(()) == ""


def test_assembly_includes_tool_related(tmp_path: Path) -> None:
    process, _repository, store, service = _runtime(
        tmp_path,
        tool_intent=ToolUseIntent(need="查一下天气"),
    )
    first = process.experience("stone", "明天天气怎样", object_ref="user")
    service.drain_for_tests()
    object_id = first.speaker.object_id
    hangs = store.list_for("stone", object_id)
    assert hangs
    process.cognition = _PlanModel(
        ResponsePlan(
            mode="respond",
            reason="回话",
            items=(ResponseItem(channel="verbal", text="好"),),
        ),
        tool_intent=None,
    )
    second = process.experience("stone", "有结果了吗", object_ref="user")
    related = second.current_state.tool_input
    assert "【工具相关】" in related
    assert "使用中" in related or "已完成" in related or "失败" in related
    assert second.current_state.tool_hot_state == ""


def test_tool_related_includes_planning_cancel_estimate_and_invalidate(
    tmp_path: Path,
) -> None:
    from jshi.tool.contract import FeedbackKind, ToolEstimate, ToolFeedback

    hang = HangStore(tmp_path / "hang.jsonl")
    service = ToolService(
        hang,
        ToolRunner(ToolModule(StubEngine()), hang),
        intake_path=tmp_path / "tool.jsonl",
    )
    # 策划中：205 在跑，尚无记挂
    planning = service.intake_store.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="查机票",
    )
    # 使用中 + 启动估价进描述（benefit 与 need 不同才写入）
    open_rec = hang.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="查杭州天气",
        command="skill:get-weather",
        kind="use",
        plan_id="plan-1",
        plan_index=1,
        plan_total=2,
    )
    hang.append_feedback(
        open_rec.task_id,
        (
            ToolFeedback(
                request_id=open_rec.request_id,
                kind=FeedbackKind.ESTIMATE,
                estimate=ToolEstimate(
                    benefit="能拿到气温",
                    downside="可能稍慢",
                ),
            ),
        ),
    )
    # 取消 → 近期失败，最终结果写清
    cancelled = hang.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="查汇率",
        command="skill:exchange-rate",
        kind="use",
    )
    hang.cancel(cancelled.task_id)

    text = service.format_tool_related_block("stone", "OBJ-A")
    assert "【工具相关】" in text
    assert "策划中" in text
    assert "正在形成可执行步骤" in text
    assert planning.need in text
    assert "启动估价：能拿到气温" in text
    assert "占位估价" not in text
    assert "多步计划第1/2步" in text
    assert "已取消，不再执行" in text
    assert "查汇率" in text

    assert service.invalidate(open_rec.task_id, reason="过时") is True
    after = service.format_tool_related_block("stone", "OBJ-A")
    assert "查杭州天气" not in after
    assert "策划中" in after  # 未失效的策划中仍在


def test_reap_stale_planning_and_open_unblocks_retry(tmp_path: Path) -> None:
    from dataclasses import replace
    from datetime import timedelta

    from jshi.tool.contract import utc_now

    hang = HangStore(tmp_path / "hang.jsonl")
    service = ToolService(
        hang,
        ToolRunner(ToolModule(StubEngine()), hang),
        intake_path=tmp_path / "tool.jsonl",
    )
    stale_plan = service.intake_store.create(
        subject_id="stone", object_id="OBJ-A", need="旧策划"
    )
    service.intake_store._records[stale_plan.intake_id] = replace(
        service.intake_store.get(stale_plan.intake_id),
        updated_at=utc_now() - timedelta(hours=2),
    )
    zombie = hang.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="僵尸在办",
        command="skill:web-search",
        kind="use",
    )
    hang._records[zombie.task_id] = replace(
        hang.get(zombie.task_id),
        updated_at=utc_now() - timedelta(hours=2),
    )

    text = service.format_tool_related_block("stone", "OBJ-A")
    assert hang.get(zombie.task_id).status == "cancelled"
    done_plan = service.intake_store.get(stale_plan.intake_id)
    assert done_plan is not None and done_plan.status == "failed"
    assert "超时" in (done_plan.plan_error or "")
    # 不应再以「策划中 / 使用中」出现
    assert "工具名称：策划中" not in text
    in_use = text.split("# 近期使用完成的工具")[0]
    assert "僵尸在办" not in in_use
    assert "已取消" in text


def test_reap_closes_open_hang_that_already_has_result(tmp_path: Path) -> None:
    """RESULT 已写入但未 notified 时，组装前应收口，不能一直「使用中」。"""
    hang = HangStore(tmp_path / "hang.jsonl")
    service = ToolService(
        hang,
        ToolRunner(ToolModule(StubEngine()), hang),
        intake_path=tmp_path / "tool.jsonl",
    )
    rec = hang.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="查汇率并检索",
        command="skill:exchange-rate",
        kind="use",
    )
    hang.set_wrap(
        rec.task_id,
        visible=False,
        summary="人民币兑美元汇率仍在查询中",
        terminal=False,
    )
    hang.append_feedback(
        rec.task_id,
        (
            ToolFeedback(
                request_id=rec.request_id,
                kind=FeedbackKind.PROGRESS,
                progress=ToolProgress(
                    stage="tool_done",
                    step="bash",
                    partial=(
                        "base=CNY quote=USD rate=0.148908 "
                        "source=open.er-api.com"
                    ),
                ),
            ),
            ToolFeedback(
                request_id=rec.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(
                    status=ToolStatus.OK,
                    summary="ls of skills junk",
                    ideal=True,
                ),
            ),
        ),
    )
    assert hang.get(rec.task_id).status == "open"
    service.reap_stale("stone", "OBJ-A")
    done = hang.get(rec.task_id)
    assert done is not None
    assert done.status == "notified"
    assert "0.148908" in done.summary
    assert "仍在查询中" not in done.summary


def test_dedupe_plan_failure_not_labeled_as_cannot_plan(tmp_path: Path) -> None:
    hang = HangStore(tmp_path / "hang.jsonl")
    service = ToolService(
        hang,
        ToolRunner(ToolModule(StubEngine()), hang),
        intake_path=tmp_path / "tool.jsonl",
    )
    rec = service.intake_store.create(
        subject_id="stone", object_id="OBJ-A", need="再查一次"
    )
    service.intake_store.update(
        rec.intake_id,
        status="failed",
        plan_error="我这边已经在办同一件事了——重新检索还在跑，我不再另开一本。",
    )
    text = service.format_tool_related_block("stone", "OBJ-A")
    assert "未新开" in text
    assert "未能形成可执行步骤" not in text
    assert "不再另开" in text


def test_recent_tool_related_keeps_last_ok_and_last_fail_per_tool(
    tmp_path: Path,
) -> None:
    """同一工具在「近期完成」里最多各留一条成功、一条失败。"""
    from datetime import timedelta

    from jshi.tool.contract import utc_now
    from jshi.tool.service import dedupe_recent_tool_entries

    older = utc_now() - timedelta(minutes=30)
    newer = utc_now() - timedelta(minutes=3)
    mid = utc_now() - timedelta(minutes=9)
    entries = [
        {
            "id": "cad-ok",
            "section": "recent",
            "status": "已完成",
            "tool_name": "skill:exchange-rate",
            "need": "兑加元",
            "used_at": newer,
            "final_result": "0.2066",
        },
        {
            "id": "usd-junk",
            "section": "recent",
            "status": "已完成",
            "tool_name": "skill:exchange-rate",
            "need": "兑美元",
            "used_at": older,
            "final_result": "---\nname: web-search",
        },
        {
            "id": "search-fail",
            "section": "recent",
            "status": "失败",
            "tool_name": "skill:web-search",
            "need": "换词重搜",
            "used_at": mid,
            "final_result": "超时",
        },
        {
            "id": "search-ok",
            "section": "recent",
            "status": "已完成",
            "tool_name": "skill:web-search",
            "need": "果蝇",
            "used_at": older,
            "final_result": "8条通称",
        },
        {
            "id": "plan-old",
            "section": "recent",
            "status": "失败",
            "tool_name": "策划",
            "need": "旧策划",
            "used_at": older,
            "final_result": "策划超时未完成",
        },
        {
            "id": "plan-new",
            "section": "recent",
            "status": "失败",
            "tool_name": "策划",
            "need": "新策划",
            "used_at": mid,
            "final_result": "策划超时未完成",
        },
        {
            "id": "open-1",
            "section": "in_use",
            "status": "使用中",
            "tool_name": "skill:exchange-rate",
            "need": "在办",
            "used_at": newer,
        },
    ]
    kept = dedupe_recent_tool_entries(entries)
    recent = [e for e in kept if e["section"] == "recent"]
    in_use = [e for e in kept if e["section"] == "in_use"]
    assert len(in_use) == 1
    assert {e["id"] for e in recent} == {
        "cad-ok",
        "search-fail",
        "search-ok",
        "plan-new",
    }
    assert "usd-junk" not in {e["id"] for e in recent}
    assert "plan-old" not in {e["id"] for e in recent}

    # 经 ToolService 组装也生效
    hang = HangStore(tmp_path / "hang.jsonl")
    service = ToolService(
        hang,
        ToolRunner(ToolModule(StubEngine()), hang),
        intake_path=tmp_path / "tool.jsonl",
    )
    for need, cmd, summary, when in (
        ("兑加元", "skill:exchange-rate", "0.2066加元", newer),
        ("兑美元坏摘要", "skill:exchange-rate", "---\nname: web-search", older),
        ("果蝇通称", "skill:web-search", "8条通称", older),
    ):
        rec = hang.create(
            subject_id="stone",
            object_id="OBJ-A",
            need=need,
            command=cmd,
            kind="use",
        )
        hang.set_wrap(rec.task_id, visible=True, summary=summary, terminal=True)
        from dataclasses import replace

        hang._records[rec.task_id] = replace(
            hang.get(rec.task_id),
            updated_at=when,
            delivered_at=when,
        )
    fail = hang.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="换词超时",
        command="skill:web-search",
        kind="use",
    )
    hang.cancel(fail.task_id)
    hang._records[fail.task_id] = replace(
        hang.get(fail.task_id),
        updated_at=mid,
    )
    text = service.format_tool_related_block("stone", "OBJ-A")
    assert "0.2066加元" in text
    assert "8条通称" in text
    assert "换词超时" in text
    assert text.count("工具名称：skill:exchange-rate") == 1
    assert "兑美元坏摘要" not in text
    assert "---" not in text or "name: web-search" not in text


def test_plan_retries_false_in_flight_when_empty(tmp_path: Path) -> None:
    from jshi.tool.intake import IntakeRecord

    hang = HangStore(tmp_path / "hang.jsonl")
    rec = hang.create(
        subject_id="stone",
        object_id="OBJ-A",
        need="查日元汇率",
        command="skill:exchange-rate",
        kind="use",
    )
    hang.set_wrap(rec.task_id, visible=True, summary="约 0.15", terminal=True)

    def payload(calls: int) -> str:
        if calls == 1:
            return json.dumps(
                {"ok": False, "error": "我这边已经在办查汇率了"},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "ask": "execute",
                "command": "echo",
                "params": {},
                "expected_result": "一句",
                "estimate": {},
            },
            ensure_ascii=False,
        )

    port = _JsonPort(payload)
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=({"name": "echo", "description": "echo"},),
        hang_store=hang,
    )
    result = planner.plan(
        IntakeRecord(
            intake_id="i1",
            subject_id="stone",
            object_id="OBJ-A",
            need="再查一次日元汇率",
        )
    )
    assert isinstance(result, ToolRequest)
    assert result.command == "echo"
    assert port.calls == 2
    assert hang.list_open("stone", "OBJ-A") == ()


def test_pi_engine_aggregates_multiple_shell_outputs() -> None:
    from jshi.tool.pi_engine import PiEngine

    lines = (
        json.dumps(
            {
                "type": "tool_execution_end",
                "toolName": "bash",
                "isError": False,
                "result": {
                    "content": [
                        {"type": "text", "text": "1 USD = 0.148571 JPY inverse\n"}
                    ]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        json.dumps(
            {
                "type": "tool_execution_end",
                "toolName": "bash",
                "isError": False,
                "result": {
                    "content": [{"type": "text", "text": "fly n. 苍蝇；飞行\n"}]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        json.dumps({"type": "agent_settled"}, ensure_ascii=False) + "\n",
    )
    engine = PiEngine()
    engine._spawn = lambda: _FakePopen(lines)
    items = list(engine.iter_execute(ToolRequest(need="查汇率并查词", command="bash")))
    results = [item for item in items if item.kind is FeedbackKind.RESULT]
    assert len(results) == 1
    content = (results[0].result.result or {}).get("content", "")
    assert "0.148571" in content
    assert "苍蝇" in content
