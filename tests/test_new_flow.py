"""最新主体流程的系统占位测试。

覆盖：阶段① 身份识别携带对象信息、阶段② 活跃区装载（空/有事件，
输入不做归属判断）、阶段⑤ 追加召回子循环（含轮次上限）、
反馈/治理占位钩子、preview 只读。
"""

from __future__ import annotations

from jshi.governance import ContinuityFinding
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse, RecallRequest
from jshi.recognition import ObjectProfile, SpeakerCandidate
from jshi.subject import (
    HistoryKind,
    SubjectProcess,
    SubjectRepository,
)


class FixedModel:
    name = "fixed-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="回应", model=self.name)


def runtime(tmp_path, model=None):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, model or FixedModel())
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="user", source="test", status="confirmed"
        )
    )
    return process, repository


class NamedRecognition:
    name = "test-recognition"

    def resolve(
        self,
        subject_id: str,
        input_text: str,
        object_ref: str | None,
        channel: str | None = None,
        carriers: tuple = (),
    ) -> SpeakerCandidate:
        return SpeakerCandidate(
            label="张三",
            object_id="OBJ-1",
            confidence=0.8,
            status="provisional",
            object_ref=object_ref,
        )


def test_recognition_carries_object_info_into_fact(tmp_path):
    process, repository = runtime(tmp_path)
    process.recognition = NamedRecognition()

    result = process.experience("stone", "你好", object_ref="user")

    fact = repository.list_history("stone", HistoryKind.FACT)[0]
    assert fact.content["source"] == "张三"
    assert fact.content["object_id"] == "OBJ-1"
    assert fact.content["object_status"] == "provisional"
    assert fact.content["object_confidence"] == 0.8
    assert result.speaker.object_id == "OBJ-1"


def test_active_zone_loads_unfinished_event_without_attribution(tmp_path):
    process, repository = runtime(tmp_path)
    concern = process.propose_open_matter(
        "stone", "继续理解朋友的疲倦", source_ids=("seed",)
    )

    result = process.experience("stone", "继续", object_ref="user")

    # 输入不做归属判断：占位模型不聚焦，活动挂载为空
    assert result.activity.active_concern_ids == ()
    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    loaded = [
        item
        for item in subject
        if item.event_type == "event_loaded"
    ]
    assert len(loaded) == 1
    assert loaded[0].content["event_ids"] == [concern.id]
    assert loaded[0].content["unfinished_ids"] == [concern.id]
    assembled = [
        item
        for item in subject
        if item.event_type == "current_state_assembled"
    ]
    assert assembled[0].content["event_ids"] == [concern.id]
    assert concern.id in result.current_state.active_event_ids


def test_first_input_empty_active_zone(tmp_path):
    process, repository = runtime(tmp_path)

    process.experience("stone", "今天有些疲倦", object_ref="user")

    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    loaded = [
        item
        for item in subject
        if item.event_type == "event_loaded"
    ]
    assert loaded[0].content["event_ids"] == []
    assembled = [
        item
        for item in subject
        if item.event_type == "current_state_assembled"
    ]
    assert assembled[0].content["event_ids"] == []


class FollowupModel:
    name = "followup-model"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="需要更多过去",
                model=self.name,
                recall_requests=(RecallRequest(query="朋友"),),
            )
        return ModelResponse(text="最终回应", model=self.name)


def test_followup_recall_extends_working_set(tmp_path):
    model = FollowupModel()
    process, repository = runtime(tmp_path, model=model)
    seeded = process.memory.remember_fact(
        "stone", "external_input", "朋友上周很忙"
    )

    result = process.experience("stone", "他最近怎么样", object_ref="user")

    assert result.thought.content == "最终回应"
    assert model.calls == 2
    extended = [
        item
        for item in repository.list_history("stone", HistoryKind.SUBJECT)
        if item.event_type == "recall_extended"
    ]
    assert len(extended) == 1
    assert seeded in extended[0].content["recalled_event_ids"]
    assert extended[0].content["request"]["query"] == "朋友"


class AlwaysRecallModel:
    name = "always-recall"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            text="还要更多",
            model=self.name,
            recall_requests=(RecallRequest(query="更多"),),
        )


def test_followup_recall_is_truncated_after_one_round(tmp_path):
    model = AlwaysRecallModel()
    process, repository = runtime(tmp_path, model=model)

    result = process.experience("stone", "你好", object_ref="user")

    assert model.calls == 2
    assert result.thought.content == "还要更多"
    metrics = [
        item
        for item in repository.list_history("stone", HistoryKind.SUBJECT)
        if item.event_type == "recall_metrics"
    ]
    assert len(metrics) == 1
    assert metrics[0].content["truncated"] is True


class RecordingFeedback:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def ingest_result(self, subject_id: str, activity_id: str, action_text: str) -> None:
        self.calls.append((subject_id, activity_id, action_text))


class CheckingGovernance:
    def check(self, subject_id, activity, fact, attribution):
        return (
            ContinuityFinding(
                severity="warning",
                message="占位检查发现",
                evidence_ids=(fact.id,),
            ),
        )


def test_placeholder_feedback_and_governance_hooks(tmp_path):
    process, repository = runtime(tmp_path)
    feedback = RecordingFeedback()
    governance = CheckingGovernance()
    process.feedback = feedback
    process.governance = governance

    result = process.experience("stone", "你好", object_ref="user")

    assert feedback.calls and feedback.calls[0][0] == "stone"
    assert feedback.calls[0][1] == result.activity.id
    checks = [
        item
        for item in repository.list_history("stone", HistoryKind.SUBJECT)
        if item.event_type == "continuity_check"
    ]
    assert len(checks) == 1
    assert checks[0].content["findings"][0]["severity"] == "warning"


def test_preview_is_read_only(tmp_path):
    process, repository = runtime(tmp_path)

    preview = process.preview_state("stone", "你好", object_ref="user")

    assert preview.speaker.status == "confirmed"
    assert preview.active_zone.events == ()
    assert preview.assembled.active_event_ids == ()
    assert repository.list_history("stone") == ()
