"""02 活跃区与活动窗口测试：不包含事件语义。"""

from __future__ import annotations

from jshi.activezone import InProcessActiveZone
from jshi.experienceledger import InProcessExperienceLedger, OutputKind
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.recognition import ObjectProfile
from jshi.subject import HistoryKind, SubjectProcess, SubjectRepository


class CountingModel:
    name = "counting-model"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(text="回应", model=self.name)


def runtime(tmp_path, model=None):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, model or CountingModel())
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="user", source="test", status="confirmed"
        )
    )
    return process, repository


def test_first_activity_window_contains_current_external(tmp_path):
    model = CountingModel()
    process, repository = runtime(tmp_path, model=model)

    result = process.experience("stone", "你好", object_ref="user")

    kinds = [segment.output_kind for segment in result.current_state.active_zone.segments]
    assert kinds == [OutputKind.EXTERNAL_INPUT]
    assert model.calls == 1


def test_second_activity_window_does_not_repeat_previous_activity(tmp_path):
    process, repository = runtime(tmp_path)

    process.experience("stone", "你好", object_ref="user")
    result = process.experience("stone", "继续", object_ref="user")

    kinds = [segment.output_kind for segment in result.current_state.active_zone.segments]
    assert kinds == [OutputKind.EXTERNAL_INPUT]


def test_context_window_loaded_is_recorded(tmp_path):
    process, repository = runtime(tmp_path)

    process.experience("stone", "你好", object_ref="user")

    loaded = [
        record
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
        if record.event_type == "context_window_loaded"
    ]
    assert len(loaded) == 1
    assert "segment_ids" in loaded[0].content


def test_active_zone_does_not_produce_event_records(tmp_path):
    process, repository = runtime(tmp_path)

    process.experience("stone", "你好", object_ref="user")

    event_types = {
        record.event_type
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
    }
    assert "event_loaded" not in event_types
    assert "event_evicted" not in event_types


def test_in_process_active_zone_uses_ledger_window():
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    ledger.append_subject_reply("stone", text_raw="reply")

    active = InProcessActiveZone(ledger, default_chars=20)
    view = active.load("stone", "继续")

    assert [segment.output_kind for segment in view.segments] == [
        OutputKind.EXTERNAL_INPUT,
        OutputKind.SUBJECT_REPLY,
    ]
