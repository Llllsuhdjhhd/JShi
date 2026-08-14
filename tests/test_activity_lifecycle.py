"""04 活动生命周期测试。

覆盖：外部/内部活动建立与完成记账、状态迁移审计、
事件挂载回填的无效标识记录、状态不变不产生迁移。
"""

from __future__ import annotations

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.recognition import ObjectProfile
from jshi.subject import (
    HistoryKind,
    SubjectProcess,
    SubjectRepository,
)


class FixedModel:
    name = "fixed-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="回应", model=self.name)


class FocusedModel:
    name = "focused-model"

    def __init__(self, *event_ids: str) -> None:
        self.event_ids = event_ids

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="回应",
            model=self.name,
            focused_event_ids=self.event_ids,
        )


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


def test_external_activity_created_and_completed(tmp_path):
    process, repository = runtime(tmp_path)

    result = process.experience("stone", "你好", object_ref="user")

    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    created = [
        record
        for record in subject
        if record.event_type == "activity_created"
    ]
    completed = [
        record
        for record in subject
        if record.event_type == "activity_completed"
    ]
    assert len(created) == 1
    assert created[0].content["activity_id"] == result.activity.id
    assert created[0].content["kind"] == "external"
    assert created[0].content["trigger"] == result.activity.trigger
    assert created[0].content["status"] == "open"
    assert len(completed) == 1
    assert completed[0].content["activity_id"] == result.activity.id
    assert completed[0].content["to"] == "completed"
    assert completed[0].content["reason"]

    transitions = repository.list_transitions(result.activity.id)
    assert [(item.target_type, item.from_state, item.to_state) for item in transitions] == [
        ("activity", "open", "completed")
    ]
    assert transitions[0].reason == "external_activity_finished_after_action"


def test_internal_activity_created_and_completed(tmp_path):
    process, repository = runtime(tmp_path)

    reflection = process.reflect("stone", "回顾一下")

    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    created = [
        record
        for record in subject
        if record.event_type == "activity_created"
        and record.content["kind"] == "internal"
    ]
    completed = [
        record
        for record in subject
        if record.event_type == "activity_completed"
    ]
    assert len(created) == 1
    assert created[0].content["activity_id"] == reflection.activity_id
    assert len(completed) == 1
    transitions = repository.list_transitions(reflection.activity_id)
    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("open", "completed")
    ]


def test_attach_records_invalid_event_ids(tmp_path):
    process, repository = runtime(tmp_path)
    concern = process.propose_open_matter(
        "stone", "聚焦的事", source_ids=("seed",)
    )
    process.cognition = FocusedModel(concern.id, "MISSING-1")

    result = process.experience("stone", "继续", object_ref="user")

    assert result.activity.active_concern_ids == (concern.id,)
    attached = [
        record
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
        if record.event_type == "activity_events_attached"
    ]
    assert attached[-1].content["event_ids"] == [concern.id]
    assert attached[-1].content["invalid_event_ids"] == ["MISSING-1"]


def test_attach_all_invalid_still_records(tmp_path):
    process, repository = runtime(tmp_path)
    process.cognition = FocusedModel("MISSING-1", "MISSING-2")

    result = process.experience("stone", "继续", object_ref="user")

    assert result.activity.active_concern_ids == ()
    attached = [
        record
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
        if record.event_type == "activity_events_attached"
    ]
    assert attached[-1].content["event_ids"] == []
    assert attached[-1].content["invalid_event_ids"] == ["MISSING-1", "MISSING-2"]


def test_status_unchanged_update_has_no_transition(tmp_path):
    process, repository = runtime(tmp_path)
    concern = process.propose_open_matter(
        "stone", "聚焦的事", source_ids=("seed",)
    )
    process.cognition = FocusedModel(concern.id)

    result = process.experience("stone", "继续", object_ref="user")

    transitions = repository.list_transitions(result.activity.id)
    # 回填挂载不改变状态，不产生迁移；只有收尾一条 open → completed
    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("open", "completed")
    ]
