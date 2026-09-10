"""记挂账本、后台执行与主链路发起（Stub；不覆盖 Pi）。"""

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
    ToolCallIntent,
)
from jshi.recognition import ObjectProfile
from jshi.skill import CognitionSkill
from jshi.subject import ActivityStatus, HistoryKind, SubjectProcess, SubjectRepository
from jshi.tool import (
    FeedbackKind,
    HangStore,
    StubEngine,
    ToolModule,
    ToolRequest,
    ToolRunner,
    ToolStatus,
)


class _PlanModel:
    name = "plan-model"

    def __init__(self, plan: ResponsePlan, tool_request: ToolCallIntent | None = None) -> None:
        self._plan = plan
        self._tool_request = tool_request

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(
            model=self.name,
            response_plan=self._plan,
            tool_request=self._tool_request,
        )


class _LatchEngine:
    """等测试放行才交出 Stub 反馈，保证 experience 返回时记挂仍为 open。"""

    name = "latch"

    def __init__(self) -> None:
        self.release = threading.Event()
        self._stub = StubEngine()

    def list_templates(self) -> tuple[str, ...]:
        return self._stub.list_templates()

    def execute(self, request: ToolRequest):
        self.release.wait(timeout=5)
        return self._stub.execute(request)


class _DummyModel:
    name = "dummy"

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        raise AssertionError("parse-only")


def _runtime(tmp_path: Path, *, tool_request: ToolCallIntent | None = None, latch: _LatchEngine | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    hang_store = HangStore(tmp_path / "hang.jsonl")
    engine = latch or StubEngine()
    runner = ToolRunner(ToolModule(engine), hang_store)
    plan = ResponsePlan(
        mode="respond",
        reason="去查",
        items=(ResponseItem(channel="verbal", text="我去查一下"),),
    )
    process = SubjectProcess(
        repository,
        identities,
        _PlanModel(plan, tool_request=tool_request),
        hang_store=hang_store,
        tool_runner=runner,
    )
    process.profiles.create(
        ObjectProfile(object_id="OBJ-USER", label="user", source="test", status="confirmed")
    )
    return process, repository, hang_store, runner


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
    assert done.notify_caller is True
    assert done.status == "notified"
    assert done.feedback[-1].result is not None
    assert done.feedback[-1].result.status is ToolStatus.OK


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


def test_process_starts_hang_and_still_speaks(tmp_path: Path) -> None:
    latch = _LatchEngine()
    process, repository, store, runner = _runtime(
        tmp_path,
        tool_request=ToolCallIntent(need="查一下天气", template="echo"),
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
    opened = store.list_open("stone", result.speaker.object_id)
    assert len(opened) == 1
    assert opened[0].need == "查一下天气"
    assert opened[0].field_ref.get("zone_kind") == "wood"
    assert "rewritten_context" not in opened[0].field_ref
    latch.release.set()
    runner.drain_for_tests()
    done = store.get(opened[0].task_id)
    assert done is not None
    assert done.notify_caller is True
    assert done.status == "notified"


def test_process_without_tool_request_leaves_hang_empty(tmp_path: Path) -> None:
    process, _repository, store, runner = _runtime(tmp_path, tool_request=None)
    result = process.experience("stone", "你好", object_ref="user")
    runner.drain_for_tests()
    assert result.activity.status == ActivityStatus.COMPLETED
    assert store.list_open("stone", result.speaker.object_id) == ()
    assert not (tmp_path / "hang.jsonl").is_file()


def test_cognition_parses_optional_tool_request() -> None:
    skill = CognitionSkill(_DummyModel())
    parsed = skill.parse(
        {
            "response_plan": {
                "mode": "respond",
                "reason": "去查",
                "items": [{"channel": "verbal", "text": "我去查一下"}],
            },
            "tool_request": {
                "need": "查天气",
                "template": "echo",
                "params": {"value": "ok"},
                "expected_result": "一句天气",
            },
        }
    )
    assert parsed.tool_request is not None
    assert parsed.tool_request.need == "查天气"
    assert parsed.tool_request.template == "echo"
    assert parsed.tool_request.params["value"] == "ok"
    assert parsed.tool_request.expected_result == "一句天气"


def test_cognition_ignores_short_or_broken_tool_request() -> None:
    skill = CognitionSkill(_DummyModel())
    short = skill.parse(
        {
            "response_plan": {"mode": "think", "items": []},
            "tool_request": {"need": " "},
        }
    )
    assert short.tool_request is None
    broken = skill.parse(
        {
            "response_plan": {"mode": "think", "items": []},
            "tool_request": "not-an-object",
        }
    )
    assert broken.tool_request is None
    omitted = skill.parse({"response_plan": {"mode": "think", "items": []}})
    assert omitted.tool_request is None


def test_instruction_mentions_optional_tool_request() -> None:
    assert "tool_request" in CognitionSkill.instruction
    schema = json.dumps(CognitionSkill.schema, ensure_ascii=False)
    assert "tool_request" in schema
