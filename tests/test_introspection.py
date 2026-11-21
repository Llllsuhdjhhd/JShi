"""300 自省：探测器、队列、现场、六问、隔离与"只落记录"的验收。

对齐《方案(coding)/300-自省.md》§4。风格与 tests/test_subject_process.py 一致（tmp_path + 模型替身）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jshi.core.param_overlay import override_snapshot
from jshi.experienceledger import InProcessExperienceLedger
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.recognition import ObjectProfile
from jshi.reflection import (
    MOMENT_AFTER_ACTIVITY,
    MOMENT_IDLE,
    NOT_EVALUATED,
    InProcessReflection,
    IntrospectionLevel,
    IntrospectionQueue,
    IntrospectionRequest,
    IntrospectionStatus,
    IntrospectionTrigger,
    IntrospectionUrgency,
    SourceContext,
    merge,
)
from jshi.reflection.sources import (
    EpistemicRevisedSource,
    EventResultSource,
    IdleSource,
)
from jshi.reflection.scene import build_scene
from jshi.subject import (
    Activity,
    ActivityKind,
    CognitiveContent,
    CognitiveKind,
    EpistemicStatus,
    EvidenceKind,
    HistoryKind,
    HistoryRecord,
    SubjectProcess,
    SubjectRepository,
)
from jshi.tool import FeedbackKind, ToolFeedback, ToolResult, ToolStatus

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

SIX_ANSWERS = {
    "what": "工具没把天气查出来。",
    "choice_and_result": "我按习惯让它去查，结果超时。",
    "why": "我能控制的是选了个慢工具；环境是网络差；偶然的是对方催得急。",
    "better": "可以先说明会慢，再决定要不要等。",
    "next_time": "再遇到要外部信息时，先估时间再开口。",
    "tool_need": True,
    "tool_capability": "查天气",
    "tool_when": "对方明确问天气时",
    "tool_worth": "值",
    "interaction_need": {
        "object_id": "OBJ-USER",
        "kind": "reply",
        "gist": "把没查成的事说清楚",
        "when": "下一轮",
    },
    "worth_learning": True,
    "candidates": [{"kind": "capability", "content": "先估时再承诺"}],
}


class IntrospectionModel:
    """自省调用返回六问 JSON；其它用途返回普通文本（主流程容忍）。"""

    name = "introspection-model"

    def __init__(self, *, payload=None, raw: str | None = None, mode: str = "respond"):
        self.payload = SIX_ANSWERS if payload is None else payload
        self.raw = raw
        self.mode = mode
        self.purposes: list[str] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.purposes.append(request.purpose)
        if request.purpose == "introspection":
            if self.raw is not None:
                return ModelResponse(text=self.raw, model=self.name)
            return ModelResponse(
                text=json.dumps(self.payload, ensure_ascii=False), model=self.name
            )
        return ModelResponse(
            text=json.dumps(
                {
                    "response_plan": {
                        "mode": self.mode,
                        "reason": "测试",
                        "items": [{"channel": "verbal", "text": "我听见了"}],
                    },
                    "context_assessment": {"remove": [], "drop_recall": [], "focus": []},
                    "rewritten_context": request.input_text,
                },
                ensure_ascii=False,
            ),
            model=self.name,
        )


def runtime(tmp_path, model=None, **options):
    # 本机用临时插件跑时 tmp_path 会在多次运行间复用：身份文件与库都换新名字，避免脏数据
    identities = IdentityRepository(tmp_path / f"identities-{uuid.uuid4().hex[:8]}.json")
    identities.create(IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。"))
    repository = SubjectRepository(tmp_path / f"subject-{uuid.uuid4().hex[:8]}.sqlite3")
    process = SubjectProcess(repository, identities, model or IntrospectionModel(), **options)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="user", source="test", status="confirmed"
        )
    )
    return process, repository


def _experience(process, text: str = "我今天有些疲倦"):
    return process.experience("stone", text, object_ref="user")


def _history_types(repository, subject_id="stone"):
    return [item.event_type for item in repository.list_history(subject_id, limit=500)]


def _subject_history(repository, subject_id="stone"):
    return repository.list_history(subject_id, HistoryKind.SUBJECT, limit=500)


def test_unmarked_exchange_reaches_six_questions_and_reopens_once(tmp_path):
    model = IntrospectionModel(payload=dict(SIX_ANSWERS, open_question="需要看下一次实际回应"))
    with override_snapshot({"INNER_ACTIVITY_MODE": 2}):
        process, repository = runtime(tmp_path, model)
        ledger = process.activity_ledger
        first = ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="我遇到分歧时会先问对方怎么想。")
        ledger.append_subject_reply("stone", text_raw="你刚才先问了我的理由。")
        request = process.introspect_idle("stone")
        assert first.segment_id in request.evidence["segment_ids"]
        run, = process.drain_introspection("stone")
        assert run.request.request_id == request.request_id
        assert process.introspect_idle("stone") is None
        assert process.drain_introspection("stone") == ()
        other = ledger.append_external("stone", actor_object_id="someone-else", text_raw="我也先开口了。")
        with pytest.raises(ValueError, match="对象"):
            process.revisit_introspection("stone", request.request_id, other.segment_id)
        second = ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="这次我直接提出意见了。")
        process.revisit_introspection("stone", run.cognitive_content_id, second.segment_id)
        process.revisit_introspection("stone", request.request_id, second.segment_id)
        revised, = process.drain_introspection("stone", limit=3)
        assert {first.segment_id, second.segment_id} <= set(revised.scene_source_ids)
        assert model.purposes == ["introspection", "introspection"]
        assert _history_types(repository).count("introspection_done") == 2


def test_shadow_and_empty_discovery_have_independent_coverage(tmp_path):
    from jshi.reflection.discovery import DiscoveryBatch, RuleDiscoveryAssistant

    class Skip:
        def discover(self, context):
            return DiscoveryBatch(coverage=({"seen": [row.segment_id for row in context.segments]},))

    process, repository = runtime(tmp_path)
    executor = process.reflection_executor
    inner = executor.inner
    process.activity_ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="平常会提前一天约定时间。")
    with override_snapshot({"INNER_ACTIVITY_MODE": 1}):
        assert inner.scan("stone", shadow=True)
        assert not inner.questions.has_pending("stone")
        assert inner.questions.cursor("stone", "ledger", shadow=True) > 0
        assert inner.questions.cursor("stone", "ledger") == 0
    with override_snapshot({"INNER_ACTIVITY_MODE": 2}):
        inner.assistant = Skip()
        assert process.introspect_idle("stone") is None
        assert inner.questions.cursor("stone", "ledger") > 0
        assert process.drain_introspection("stone") == ()
        inner.assistant = RuleDiscoveryAssistant()
        process.activity_ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="今天临时约了一次。")
        for _ in range(4):
            executor.tick("stone", force=True)
            if "introspection_done" in _history_types(repository):
                break
        assert _history_types(repository).count("introspection_done") == 1


def test_inner_six_replays_after_history_written_without_duplicate_call(tmp_path, monkeypatch):
    model = IntrospectionModel()
    with override_snapshot({"INNER_ACTIVITY_MODE": 2}):
        process, repository = runtime(tmp_path, model)
        process.activity_ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="我每次都把顺序写下来。")
        process.introspect_idle("stone")
        inner = process.reflection_executor.inner
        complete = inner.questions.complete
        def interrupted(*args, **kwargs):
            raise RuntimeError("interrupted after subject history")
        monkeypatch.setattr(inner.questions, "complete", interrupted)
        assert process.drain_introspection("stone") == ()
        assert _history_types(repository).count("introspection_done") == 1
        monkeypatch.setattr(inner.questions, "complete", complete)
        assert len(process.drain_introspection("stone")) == 1
        assert process.drain_introspection("stone") == ()
        assert model.purposes == ["introspection"]
        assert _history_types(repository).count("introspection_done") == 1


def test_late_tool_result_and_activity_close_share_one_owner(tmp_path):
    with override_snapshot({"INNER_ACTIVITY_MODE": 2}):
        process, repository = runtime(tmp_path)
        activity = Activity(subject_id="stone", kind=ActivityKind.INTERNAL, trigger="天气")
        repository.add_activity(activity)
        def close():
            process._maybe_introspect_after_activity("stone", activity,
                SimpleNamespace(reason="查天气", mode="respond"), "", SimpleNamespace(object_id="OBJ-USER"))
        hang = process.hang_store.create(subject_id="stone", object_id="OBJ-USER", need="查天气",
            template="", field_ref={"activity_id": activity.id}, request_id="late")
        close()
        process.hang_store.append_feedback(hang.task_id, (ToolFeedback(kind=FeedbackKind.RESULT,
            result=ToolResult(status=ToolStatus.FAILED, ideal=False, ideal_note="超时", summary="没查成")),))
        close()
        inner = process.reflection_executor.inner
        inner.scan_signals("stone")
        inner.scan("stone")
        runs = process.drain_introspection("stone", limit=3)
        assert len(runs) == 1 and runs[0].request.evidence["tool_facts"]
        assert not process.reflection.queue.pending()
        assert _history_types(repository).count("introspection_done") == 1


def test_revised_cognition_enters_discovery_without_another_conversation(tmp_path):
    with override_snapshot({"INNER_ACTIVITY_MODE": 2}):
        process, repository = runtime(tmp_path)
        cognition = CognitiveContent(subject_id="stone", activity_id="a", kind=CognitiveKind.INFERENCE,
            content="我猜他不高兴。", epistemic_status=EpistemicStatus.PROVISIONAL,
            evidence_kind=EvidenceKind.COGNITIVE_REASONING)
        repository.add_cognitive_content(cognition)
        process.transition_cognition(cognition.id, EpistemicStatus.REVISED, "他解释是因为没睡好。")
        question, = process.reflection_executor.inner.scan_signals("stone")
        assert question.entry_ref == f"cognition:{cognition.id}"
        assert question.object_id is None
        run, = process.drain_introspection("stone")
        assert run.scene_source_ids and run.request.request_id == question.request_id


def test_conversation_interrupts_six_and_resumes_with_same_visible_scene(tmp_path):
    active = False
    calls = []

    class Interrupted(IntrospectionModel):
        def generate(self, request):
            nonlocal active
            calls.append(request.input_text)
            active = len(calls) == 1
            return super().generate(request)

    with override_snapshot({"INNER_ACTIVITY_MODE": 2}):
        process, repository = runtime(tmp_path, Interrupted())
        process.activity_ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="先对照两个人的原话。")
        process.introspect_idle("stone")
        executor = process.reflection_executor
        assert not executor.tick("stone", force=True, busy=lambda: active)
        assert "introspection_done" not in _history_types(repository)
        assert executor.inner.questions.has_pending("stone")
        active = False
        process.activity_ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="这是后来才说的旁话。")
        runs = process.drain_introspection()
        assert len(runs) == 1 and len(calls) == 2 and calls[0] == calls[1]
        assert _history_types(repository).count("introspection_done") == 1


def test_cli_attaches_real_segment_using_manual_reflect_content_id(tmp_path, monkeypatch, capsys):
    from jshi.app import cli
    import sys

    with override_snapshot({"INNER_ACTIVITY_MODE": 2}):
        process, repository = runtime(tmp_path)
        ledger = process.activity_ledger
        ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="平时我会先问清缘由。")
        result = process.reflect("stone", "我有没有听清他的理由？")
        second = ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="第二次我先用了自己的解释。")
        monkeypatch.setattr(cli, "_runtime", lambda *a, **kw: (process, None, repository))
        monkeypatch.setattr(sys, "argv", ["jshi", "--data-dir", str(tmp_path), "introspect-followup", "stone",
                                          result.id, second.segment_id])
        cli.main()
        assert "已关联原段" in capsys.readouterr().out
        revised, = process.drain_introspection("stone")
        assert second.segment_id in revised.scene_source_ids


# ----------------------------------------------------------------------
# 探测器（只读）
# ----------------------------------------------------------------------


def _context(**overrides):
    base = dict(
        subject_id="stone",
        moment=MOMENT_AFTER_ACTIVITY,
        now=NOW,
        repository=None,
        ledger=None,
        memory=None,
        hang_store=None,
        activity=None,
        plan=None,
        action_id="",
        object_id="OBJ-USER",
    )
    base.update(overrides)
    return SourceContext(**base)


class _HangStore:
    def __init__(self, records):
        self._records = records

    def list_for(self, subject_id, object_id):
        return tuple(self._records)


def _hang(activity_id: str, status=ToolStatus.FAILED, **extra):
    result = ToolResult(
        status=status,
        ideal=extra.get("ideal", False),
        ideal_note=extra.get("ideal_note", "超时"),
        summary=extra.get("summary", "没查成"),
    )
    return SimpleNamespace(
        field_ref={"activity_id": activity_id},
        feedback=(ToolFeedback(kind=FeedbackKind.RESULT, result=result),),
    )


def test_event_result_source_triggers_on_failed_tool():
    activity = SimpleNamespace(id="A-1", updated_at=NOW)
    ctx = _context(
        activity=activity,
        plan=SimpleNamespace(reason="想查天气"),
        hang_store=_HangStore([_hang("A-1")]),
    )

    found = EventResultSource().detect(ctx)

    assert len(found) == 1
    request = found[0]
    assert request.trigger is IntrospectionTrigger.EVENT_RESULT
    assert request.entry_ref == "activity:A-1"
    assert request.evidence["tool_failures"][0]["status"] == "failed"


def test_event_result_source_ignores_ok_tool_and_think_mode():
    activity = SimpleNamespace(id="A-2", updated_at=NOW)
    ok_ctx = _context(
        activity=activity,
        plan=SimpleNamespace(reason="普通一轮"),
        hang_store=_HangStore(
            [_hang("A-2", status=ToolStatus.OK, ideal=True, ideal_note="")]
        ),
    )
    assert EventResultSource().detect(ok_ctx) == ()

    # think 只作为计划模式出现，不作信号（方案 §3.2 / §5.14）
    think_ctx = _context(
        activity=activity,
        plan=SimpleNamespace(reason="我心里盘算", mode="think"),
        hang_store=None,
    )
    assert EventResultSource().detect(think_ctx) == ()


def test_event_result_source_ignores_other_activity_hangs():
    activity = SimpleNamespace(id="A-3", updated_at=NOW)
    ctx = _context(
        activity=activity,
        plan=SimpleNamespace(reason="x"),
        hang_store=_HangStore([_hang("A-other")]),
    )
    assert EventResultSource().detect(ctx) == ()


def test_epistemic_source_detects_revised_and_carries_two_time_centers(tmp_path):
    repository = SubjectRepository(tmp_path / f"subject-{uuid.uuid4().hex[:8]}.sqlite3")
    cognition = CognitiveContent(
        subject_id="stone",
        activity_id="A-0",
        kind=CognitiveKind.INFERENCE,
        content="他今天不高兴，是因为我说错了话。",
        epistemic_status=EpistemicStatus.CONSIDERING,
        evidence_kind=EvidenceKind.COGNITIVE_REASONING,
    )
    repository.add_cognitive_content(cognition)
    repository.add_history(
        HistoryRecord(
            subject_id="stone",
            kind=HistoryKind.SUBJECT,
            event_type="epistemic_transition",
            content={
                "cognitive_content_id": cognition.id,
                "from": "considering",
                "to": "revised",
                "reason": "后来知道他是累了",
            },
        )
    )

    ctx = _context(repository=repository, moment=MOMENT_IDLE)
    found = EpistemicRevisedSource().detect(ctx)

    assert len(found) == 1
    request = found[0]
    assert request.trigger is IntrospectionTrigger.EPISTEMIC_REVISED
    assert request.entry_ref == f"cognition:{cognition.id}"
    assert request.entry_at_also == cognition.created_at
    assert request.origin == "manual_transition"

    repository.add_history(
        HistoryRecord(
            subject_id="stone", kind=HistoryKind.SUBJECT, event_type="activity_completed", content={}
        )
    )
    with override_snapshot({"INTROSPECTION_TRANSITION_HISTORY_LIMIT": 1}):
        assert EpistemicRevisedSource().detect(ctx) == ()
    with override_snapshot({"INTROSPECTION_TRANSITION_HISTORY_LIMIT": 2}):
        assert EpistemicRevisedSource().detect(ctx)[0].entry_ref == request.entry_ref


def test_idle_source_takes_earliest_unseen_segment_in_window():
    ledger = InProcessExperienceLedger()
    repository = SubjectRepository.__new__(SubjectRepository)  # 只需 list_history
    repository.list_history = lambda subject_id, limit=100: ()  # type: ignore[method-assign]
    for index in range(3):
        ledger.append_external(
            "stone", actor_object_id="OBJ-USER", text_raw=f"第 {index} 段"
        )

    found = IdleSource().detect(
        _context(moment=MOMENT_IDLE, ledger=ledger, repository=repository)
    )

    assert len(found) == 1
    assert found[0].trigger is IntrospectionTrigger.IDLE
    assert found[0].level is IntrospectionLevel.LOW
    assert found[0].entry_text == "第 0 段"


def test_idle_source_skips_already_introspected():
    ledger = InProcessExperienceLedger()
    first = ledger.append_external(
        "stone", actor_object_id="OBJ-USER", text_raw="第一段"
    )
    ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="第二段")

    class _Repo:
        @staticmethod
        def list_history(subject_id, limit=100):
            return (
                HistoryRecord(
                    subject_id="stone",
                    kind=HistoryKind.SUBJECT,
                    event_type="introspection_done",
                    content={"entry_ref": f"segment:{first.segment_id}"},
                ),
            )

    found = IdleSource().detect(
        _context(moment=MOMENT_IDLE, ledger=ledger, repository=_Repo())
    )

    assert len(found) == 1
    assert found[0].entry_text == "第二段"


def test_idle_source_uses_independent_segment_and_history_windows():
    ledger = InProcessExperienceLedger()
    segments = [
        ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw=text)
        for text in ("甲", "乙", "丙")
    ]
    history = [
        SimpleNamespace(
            event_type="introspection_done", content={"entry_ref": f"segment:{segments[1].segment_id}"}
        ),
        SimpleNamespace(event_type="activity_completed", content={}),
    ]
    repo = SimpleNamespace(list_history=lambda subject_id, limit: history[-limit:])
    ctx = _context(moment=MOMENT_IDLE, ledger=ledger, repository=repo)
    for window, scan, expected in [(2, 1, "乙"), (2, 2, "丙"), (3, 2, "甲")]:
        with override_snapshot({
            "INTROSPECTION_IDLE_WINDOW": window,
            "INTROSPECTION_IDLE_HISTORY_LIMIT": scan,
        }):
            assert IdleSource().detect(ctx)[0].entry_text == expected


def test_merge_keeps_one_introspection_per_record():
    shared = dict(
        subject_id="stone",
        entry_ref="activity:A-9",
        entry_at=NOW,
    )
    first = IntrospectionRequest(
        trigger=IntrospectionTrigger.EVENT_RESULT,
        level=IntrospectionLevel.MEDIUM,
        urgency=IntrospectionUrgency.SLOW,
        entry_text="短",
        evidence={"tool_failures": [{"status": "failed"}]},
        **shared,
    )
    second = IntrospectionRequest(
        trigger=IntrospectionTrigger.EPISTEMIC_REVISED,
        level=IntrospectionLevel.HIGH,
        urgency=IntrospectionUrgency.URGENT,
        entry_text="更长的一条条目文本",
        evidence={"to": "revised"},
        **shared,
    )

    merged = merge((first, second))

    assert len(merged) == 1
    assert merged[0].level is IntrospectionLevel.HIGH
    assert merged[0].urgency is IntrospectionUrgency.URGENT
    assert merged[0].entry_text == "更长的一条条目文本"
    assert len(merged[0].evidence["merged"]) == 2


# ----------------------------------------------------------------------
# 队列
# ----------------------------------------------------------------------


def _request(theme: str, urgency=IntrospectionUrgency.SOON, level=IntrospectionLevel.MEDIUM):
    return IntrospectionRequest(
        subject_id="stone",
        trigger=IntrospectionTrigger.IDLE,
        entry_ref=theme,
        entry_at=NOW,
        level=level,
        urgency=urgency,
    )


def test_queue_orders_by_urgency_then_arrival():
    queue = IntrospectionQueue(max_size=5, cooldown_seconds=60, now=lambda: NOW)
    slow = _request("slow", IntrospectionUrgency.SLOW)
    urgent = _request("urgent", IntrospectionUrgency.URGENT)
    soon = _request("soon", IntrospectionUrgency.SOON)
    for request in (slow, urgent, soon):
        assert queue.enqueue(request).status is IntrospectionStatus.QUEUED

    order = [queue.pop_next().theme for _ in range(3)]

    assert order == ["urgent", "soon", "slow"]


def test_queue_cooldown_and_duplicate():
    now = {"value": NOW}
    queue = IntrospectionQueue(
        max_size=5, cooldown_seconds=60, now=lambda: now["value"]
    )
    request = _request("theme-1")
    queue.enqueue(request)
    queue.finish(request)

    again = queue.enqueue(_request("theme-1"))
    assert again.status is IntrospectionStatus.DROPPED
    assert again.reason == "cooldown"

    now["value"] = NOW + timedelta(seconds=120)
    same = queue.enqueue(_request("theme-1"))
    assert same.reason == "duplicate"

    deeper = queue.enqueue(
        _request("theme-1", IntrospectionUrgency.URGENT, IntrospectionLevel.HIGH)
    )
    assert deeper.status is IntrospectionStatus.QUEUED


def test_queue_full_drops_the_slowest():
    queue = IntrospectionQueue(max_size=1, cooldown_seconds=0, now=lambda: NOW)
    queue.enqueue(_request("slow", IntrospectionUrgency.SLOW))

    rejected = queue.enqueue(_request("also-slow", IntrospectionUrgency.SLOW))
    assert rejected.reason == "queue_full"

    accepted = queue.enqueue(_request("urgent", IntrospectionUrgency.URGENT))
    assert accepted.status is IntrospectionStatus.QUEUED
    assert accepted.evicted is not None
    assert accepted.evicted.theme == "slow"


# ----------------------------------------------------------------------
# 现场
# ----------------------------------------------------------------------


class _Memory:
    def __init__(self):
        self.queries: list[str] = []

    def recall(self, subject_id, query, **kwargs):
        self.queries.append(query)
        return (SimpleNamespace(event_id="M-1", content="以前也这样", text="以前也这样"),)


def test_scene_takes_window_by_time_and_recalls_once():
    ledger = InProcessExperienceLedger()
    outside = ledger.append_external(
        "stone", actor_object_id="OBJ-USER", text_raw="很久以前", occurred_at=NOW - timedelta(hours=5)
    )
    inside_before = ledger.append_external(
        "stone", actor_object_id="OBJ-USER", text_raw="刚说的话", occurred_at=NOW - timedelta(minutes=3)
    )
    inside_after = ledger.append_external(
        "stone", actor_object_id="OBJ-USER", text_raw="后来的话", occurred_at=NOW + timedelta(minutes=4)
    )
    memory = _Memory()
    request = IntrospectionRequest(
        subject_id="stone",
        trigger=IntrospectionTrigger.EVENT_RESULT,
        entry_ref="activity:A-1",
        entry_at=NOW,
        entry_text="想查天气",
        object_id="OBJ-USER",
    )

    scene = build_scene(request, ledger=ledger, memory=memory, window_seconds=600)

    assert scene is not None
    ids = [segment.segment_id for segment in scene.segments]
    assert inside_before.segment_id in ids and inside_after.segment_id in ids
    assert outside.segment_id not in ids
    assert memory.queries == ["想查天气"]
    assert scene.recalled
    assert "现场" in scene.text


def test_scene_returns_none_without_material():
    ledger = InProcessExperienceLedger()
    request = IntrospectionRequest(
        subject_id="stone",
        trigger=IntrospectionTrigger.IDLE,
        entry_ref="segment:missing",
        entry_at=NOW,
        entry_text="",
    )

    assert build_scene(request, ledger=ledger, memory=_Memory()) is None


# ----------------------------------------------------------------------
# 一次自省（经 process）
# ----------------------------------------------------------------------


def test_reflect_manual_writes_six_questions(tmp_path):
    model = IntrospectionModel()
    process, repository = runtime(tmp_path, model)
    _experience(process)

    result = process.reflect("stone", "回顾刚才的理解")

    types = _history_types(repository)
    assert "introspection_queued" in types
    assert "introspection_done" in types
    assert "introspection_candidate" in types
    assert "③ 为什么（归因）" in result.content
    assert "⑤b 要不要借助外部工具：是" in result.content
    done = [item for item in _subject_history(repository) if item.event_type == "introspection_done"]
    assert done[-1].content["answer_mode"] == "model"


def test_reflect_degrades_on_bad_json(tmp_path):
    process, repository = runtime(tmp_path, IntrospectionModel(raw="不是 JSON"))
    _experience(process)

    result = process.reflect("stone", "回顾一下")

    assert "结构化解析失败" in result.content
    done = [item for item in _subject_history(repository) if item.event_type == "introspection_done"]
    assert done[-1].content["degraded"] is True


def test_behavior_learning_can_reach_personal_world_without_event_memory(tmp_path):
    class Model(IntrospectionModel):
        def generate(self, request):
            if request.purpose == 'introspection' and request.input_text.startswith('{"samples":'):
                packet = json.loads(request.input_text)
                key, = [sample['id'] for sample in packet['samples']]
                return ModelResponse(model=self.name, text=json.dumps({
                    'ordered_ids': [key], 'reasons': [{'id': key, 'reason': '用于以后估时'}],
                    'standard': '', 'admit_ids': [key], 'discard_ids': [],
                    'outlets': [{'id': key, 'kinds': ['personal_experience']}]}))
            return super().generate(request)
    process, _ = runtime(tmp_path, Model())
    _experience(process)
    process.reflect('stone', '回顾刚才的选择')
    store = process.reflection_memory
    assert store.pending_thought('stone')['mode'] == 'review_behavior_result'
    assert not store.experiences('stone', 'personal_experience')
    assert process.reflection_executor.tick('stone', force=True)
    assert store.pending_thought('stone') is None
    current = process.assemble_current_state('stone', '继续')
    personal = [item for item in current.personal_items if item.entry_type == 'personal_experience']
    assert len(personal) == 1 and personal[0].source_ids
    assert '先估时再承诺' in personal[0].content
    assert not store.recall_insights('stone', '先估时再承诺', None, 4)


def test_manual_reflection_routes_a_cited_encounter_to_the_correct_person(tmp_path):
    class Model(IntrospectionModel):
        def generate(self, request):
            if request.purpose == 'introspection':
                if request.input_text.startswith('{"samples":'):
                    sample, = json.loads(request.input_text)['samples']
                    assert sample['person'] == 'P1' and sample['sources'] == ['E1']
                    assert '我今天有些疲倦' in sample['excerpts']['E1']
                    return ModelResponse(model=self.name, text=json.dumps({
                        'ordered_ids': ['S1'], 'reasons': [{'id': 'S1', 'reason': '只适用于这次交流'}],
                        'standard': '', 'admit_ids': ['S1'], 'discard_ids': [],
                        'outlets': [{'id': 'S1', 'kinds': ['person_experience']}]}))
                assert '[E1]' in request.input_text and 'OBJ-USER' not in request.input_text
                self.payload = dict(SIX_ANSWERS, candidates=[{'content': '这次需要给对方留出回答时间',
                                                            'source_refs': ['E1']}])
            return super().generate(request)
    process, _ = runtime(tmp_path, Model())
    process.activity_ledger.append_external('stone', actor_object_id='OBJ-USER', text_raw='我今天有些疲倦')
    process.reflect('stone', '回顾这次交谈')
    assert process.reflection_executor.tick('stone', force=True)
    entry, = process.reflection_memory.experiences('stone', 'person_experience', object_id='OBJ-USER')
    assert entry['source_ids'] == [process.activity_ledger.list_experiences('stone')[0].segment_id]
    assert not process.reflection_memory.experiences('stone', 'personal_experience')


def test_replacing_result_evaluator_controls_behavior_without_changing_runner(tmp_path):
    from jshi.reflection.result_evaluation import ReflectionResultEvaluator
    class OnlyToolSuggestions(ReflectionResultEvaluator):
        def behavior_outlets(self, answer):
            return ['tool']
    process, repo = runtime(tmp_path, reflection_result_evaluator=OnlyToolSuggestions())
    _experience(process)
    process.reflect('stone', '回顾一下')
    candidate = next(r for r in _subject_history(repo) if r.event_type == 'introspection_candidate')
    assert candidate.content['suggested_kinds'] == ['tool']
    assert process.reflection_memory.pending_thought('stone') is None


def test_candidate_records_needs_without_writing_personal_world(tmp_path):
    process, repository = runtime(tmp_path, IntrospectionModel())
    _experience(process)
    before = len(repository.list_personal_items("stone"))

    process.reflect("stone", "回顾一下")

    candidates = [
        item for item in _subject_history(repository) if item.event_type == "introspection_candidate"
    ]
    assert candidates
    payload = dict(candidates[-1].content)
    assert payload["suggested_kinds"] == ["learn", "tool", "commitment"]
    assert payload["interaction_need"]["kind"] == "reply"
    assert len(repository.list_personal_items("stone")) == before
    assert process.tool_service.intake_store.list_for_subject("stone") == ()  # 不发 intake


def test_idle_path_enqueues_then_drains_and_dedups(tmp_path):
    model = IntrospectionModel()
    process, repository = runtime(tmp_path, model)
    _experience(process)

    refs: list[str] = []
    for _ in range(10):
        request = process.introspect_idle("stone")
        if request is None:
            break
        refs.append(request.entry_ref)
        runs = process.drain_introspection("stone", limit=1)
        assert runs and runs[0].status is IntrospectionStatus.DONE

    assert refs, "闲时回顾应当至少入队一次"
    assert len(set(refs)) == len(refs), "同一段不该重复自省"
    # 全部段都回顾过之后，闲时入口空转（不是再挑一段）
    assert process.introspect_idle("stone") is None


def test_low_level_runs_rule_summary_without_model_call(tmp_path):
    model = IntrospectionModel()
    process, repository = runtime(tmp_path, model)
    _experience(process)
    process.introspect_idle("stone")
    model.purposes.clear()

    runs = process.drain_introspection("stone", limit=1)

    assert runs[0].answer.mode == "rule"
    assert NOT_EVALUATED in runs[0].answer.choice_and_result
    assert "introspection" not in model.purposes  # 低档不调模型


def test_drop_when_no_material(tmp_path):
    process, repository = runtime(tmp_path)
    engine = process.reflection
    assert isinstance(engine, InProcessReflection)
    request = IntrospectionRequest(
        subject_id="stone",
        trigger=IntrospectionTrigger.IDLE,
        entry_ref="segment:none",
        entry_at=NOW,
        entry_text="",
    )

    engine.enqueue(request)
    runs = engine.drain("stone")

    assert runs[0].status is IntrospectionStatus.DROPPED
    assert runs[0].reason == "no_material"
    dropped = [item for item in _subject_history(repository) if item.event_type == "introspection_dropped"]
    assert dropped[-1].content["reason"] == "no_material"


def test_after_activity_hook_enqueues_tool_failure(tmp_path):
    process, repository = runtime(tmp_path)
    activity = Activity(subject_id="stone", kind=ActivityKind.INTERNAL, trigger="t")
    repository.add_activity(activity)
    hang = process.hang_store.create(
        subject_id="stone",
        object_id="OBJ-USER",
        need="查天气",
        template="",
        field_ref={"activity_id": activity.id},
        request_id="req-1",
    )
    process.hang_store.append_feedback(
        hang.task_id,
        (ToolFeedback(kind=FeedbackKind.RESULT, result=ToolResult(
            status=ToolStatus.FAILED, ideal=False, ideal_note="超时", summary="没查成"
    )),),
    )

    process._maybe_introspect_after_activity(
        "stone",
        activity,
        SimpleNamespace(reason="想查天气", mode="respond"),
        "",
        SimpleNamespace(object_id="OBJ-USER"),
    )

    queued = [item for item in _subject_history(repository) if item.event_type == "introspection_queued"]
    assert queued and queued[-1].content["trigger"] == "event_result"


def test_introspection_is_isolated_from_main_flow_state(tmp_path):
    process, repository = runtime(tmp_path)
    _experience(process)
    segments_before = len(process.activity_ledger.list_experiences("stone"))
    view_before = process.activity_ledger.current_context_view("stone")
    items_before = len(repository.list_personal_items("stone"))

    process.reflect("stone", "回顾一下")

    assert len(process.activity_ledger.list_experiences("stone")) == segments_before
    view_after = process.activity_ledger.current_context_view("stone")
    assert view_after.version == view_before.version
    assert len(repository.list_personal_items("stone")) == items_before


def test_engine_can_be_disabled(tmp_path):
    model = IntrospectionModel()
    process, repository = runtime(tmp_path, model)
    engine = process.reflection
    assert isinstance(engine, InProcessReflection)
    engine._enabled = False
    _experience(process)

    dropped = [item for item in _subject_history(repository) if item.event_type == "introspection_dropped"]
    assert dropped == []
    request = engine.enqueue(
        IntrospectionRequest(
            subject_id="stone",
            trigger=IntrospectionTrigger.IDLE,
            entry_ref="segment:x",
            entry_at=NOW,
        )
    )
    assert request is IntrospectionStatus.DROPPED
