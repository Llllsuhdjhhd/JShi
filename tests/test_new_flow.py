"""最新主体流程的系统占位测试。

覆盖：阶段① 身份识别携带对象信息、阶段② 归属判断（单关切延续/未命中）、
阶段⑤ 追加召回子循环（含轮次上限）、反馈/治理占位钩子、preview 只读。
"""

from __future__ import annotations

from jshi.attribution import AttributionResult
from jshi.governance import ContinuityFinding
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse, RecallRequest
from jshi.recognition import ObjectProfile, SpeakerCandidate
from jshi.subject import (
    HistoryKind,
    SubjectProcess,
    SubjectRepository,
)
from jshi.subject.process import FOLLOWUP_RECALL_MAX_ROUNDS


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


def test_attribution_single_concern_continuation(tmp_path):
    process, repository = runtime(tmp_path)
    concern = process.propose_open_matter(
        "stone", "继续理解朋友的疲倦", source_ids=("seed",)
    )

    result = process.experience("stone", "继续", object_ref="user")

    assert result.attribution.concern_ids == (concern.id,)
    assert concern.id in result.activity.active_concern_ids
    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    attributed = [
        item
        for item in subject
        if item.event_type == "input_attributed"
    ]
    assert len(attributed) == 1
    assert attributed[0].content["concern_ids"] == [concern.id]
    assembled = [
        item
        for item in subject
        if item.event_type == "current_state_assembled"
    ]
    assert assembled[0].content["mode"] == "concern_centric"
    assert assembled[0].content["attributed_concern_ids"] == [concern.id]


def test_full_assembly_when_no_concern(tmp_path):
    process, repository = runtime(tmp_path)

    process.experience("stone", "今天有些疲倦", object_ref="user")

    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    assembled = [
        item
        for item in subject
        if item.event_type == "current_state_assembled"
    ]
    assert assembled[0].content["mode"] == "full"
    assert assembled[0].content["attributed_concern_ids"] == []
    assert assembled[0].content["open_matter_ids"] == []


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


def test_followup_recall_rounds_are_bounded(tmp_path):
    model = AlwaysRecallModel()
    process, _repository = runtime(tmp_path, model=model)

    result = process.experience("stone", "你好", object_ref="user")

    assert model.calls == FOLLOWUP_RECALL_MAX_ROUNDS + 1
    assert result.thought.content == "还要更多"


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
    assert preview.attribution == AttributionResult()
    assert preview.mode == "full"
    assert repository.list_history("stone") == ()
