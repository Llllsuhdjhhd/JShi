"""04 活动生命周期：一轮一活动，关闭为 completed。"""

from __future__ import annotations

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.recognition import ObjectProfile
from jshi.subject import HistoryKind, SubjectProcess, SubjectRepository


class FixedModel:
    name = "fixed-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="回应", model=self.name)


def runtime(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, FixedModel())
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
    created = [r for r in subject if r.event_type == "activity_created"]
    completed = [r for r in subject if r.event_type == "activity_completed"]
    assert len(created) == 1
    assert created[0].content["kind"] == "external"
    assert len(completed) == 1
    assert completed[0].content["to"] == "completed"

    transitions = repository.list_transitions(result.activity.id)
    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("open", "completed")
    ]


def test_internal_activity_created_and_completed(tmp_path):
    process, repository = runtime(tmp_path)

    reflection = process.reflect("stone", "回顾一下")

    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    created = [
        r
        for r in subject
        if r.event_type == "activity_created" and r.content["kind"] == "internal"
    ]
    completed = [r for r in subject if r.event_type == "activity_completed"]
    assert len(created) == 1
    assert len(completed) == 1
    transitions = repository.list_transitions(reflection.activity_id)
    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("open", "completed")
    ]


def test_next_input_creates_new_activity(tmp_path):
    process, _repository = runtime(tmp_path)

    first = process.experience("stone", "你好", object_ref="user")
    second = process.experience("stone", "继续", object_ref="user")

    assert first.activity.id != second.activity.id
    assert first.activity.status.value == "completed"
    assert second.activity.status.value == "completed"
    assert first.activity.trigger != second.activity.trigger
