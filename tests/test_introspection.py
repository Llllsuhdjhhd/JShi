"""300 自省：探测器、队列、现场、六问、隔离与"只落记录"的验收。

对齐《方案(coding)/300-自省.md》§4。风格与 tests/test_subject_process.py 一致（tmp_path + 模型替身）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


def runtime(tmp_path, model=None):
    # 本机用临时插件跑时 tmp_path 会在多次运行间复用：身份文件与库都换新名字，避免脏数据
    identities = IdentityRepository(tmp_path / f"identities-{uuid.uuid4().hex[:8]}.json")
    identities.create(IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。"))
    repository = SubjectRepository(tmp_path / f"subject-{uuid.uuid4().hex[:8]}.sqlite3")
    process = SubjectProcess(repository, identities, model or IntrospectionModel())
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
