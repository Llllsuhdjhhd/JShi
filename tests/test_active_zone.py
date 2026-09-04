"""02：只读 16 的既往 ContextViewState；初次与复位走同一 load。"""

from __future__ import annotations

from jshi.activezone import InProcessActiveZone
from jshi.experienceledger import InProcessExperienceLedger
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
        return ModelResponse(
            text="回应",
            model=self.name,
            rewritten_context=request.input_text,
        )


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


def test_first_activity_reads_empty_context_view(tmp_path):
    model = CountingModel()
    process, _repository = runtime(tmp_path, model=model)

    result = process.experience("stone", "你好", object_ref="user")

    assert result.current_state.context_view.context_text == ""
    assert result.current_state.context_view.segment_refs == ()
    assert model.calls == 1
    applied = process.activity_ledger.current_context_view("stone")
    assert "你好" in applied.context_text


def test_second_activity_reads_previous_applied_view(tmp_path):
    process, _repository = runtime(tmp_path)

    process.experience("stone", "你好", object_ref="user")
    result = process.experience("stone", "继续", object_ref="user")

    text = result.current_state.context_view.context_text
    assert "你好" in text
    assert "继续" not in text


def test_context_window_loaded_is_recorded(tmp_path):
    process, repository = runtime(tmp_path)

    process.experience("stone", "你好", object_ref="user")

    loaded = [
        record
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
        if record.event_type == "context_window_loaded"
    ]
    assert len(loaded) == 1
    assert "chars" in loaded[0].content
    assert "version" in loaded[0].content


def test_active_zone_does_not_produce_event_records(tmp_path):
    process, repository = runtime(tmp_path)

    process.experience("stone", "你好", object_ref="user")

    event_types = {
        record.event_type
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
    }
    assert "event_loaded" not in event_types
    assert "event_evicted" not in event_types


def test_in_process_active_zone_only_reads_current_view():
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    ledger.append_subject_reply("stone", text_raw="reply")

    active = InProcessActiveZone(ledger)
    assert active.load("stone").context_text == ""

    ledger.apply_context_assessment("stone")
    view = active.load("stone")
    assert "one" in view.context_text
    assert "reply" in view.context_text


def test_unknown_subject_and_reset_share_empty_load():
    filled = InProcessExperienceLedger()
    filled.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    filled.append_subject_reply("stone", text_raw="reply")
    filled.apply_context_assessment("stone")
    assert InProcessActiveZone(filled).load("stone").context_text

    reset = InProcessActiveZone(InProcessExperienceLedger())
    empty = reset.load("stone")
    unknown = reset.load("other")
    assert empty.context_text == ""
    assert empty.segment_refs == ()
    assert unknown.context_text == ""
    assert unknown.version == 0
