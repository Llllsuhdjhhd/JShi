"""落位补对象映射表；30 填 interlocutor。对应 方案(coding)/16-30-回忆对象绑定.md。"""

from __future__ import annotations

from jshi.experienceledger import (
    InProcessExperienceLedger,
    OutputKind,
)
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.memorycontrol import InProcessMemoryControl
from jshi.models import ModelRequest, ModelResponse, ResponsePlan
from jshi.recognition import ObjectProfile
from jshi.subject import HistoryKind, SubjectProcess, SubjectRepository


class FixedModel:
    name = "fixed-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="回应", model=self.name)


class WaitModel:
    name = "wait-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(
            model=self.name,
            response_plan=ResponsePlan(mode="wait", reason="等对方下一条"),
        )


class _NoopMemory:
    def ingest_batch(self, batch):
        raise AssertionError("build_batch should not ingest")


def runtime(tmp_path, model=None):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, model or FixedModel())
    return process, repository


def test_experience_fills_objects_without_envelope_mapping(tmp_path):
    process, repository = runtime(tmp_path)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-LUX",
            label="lux",
            aliases=("路光",),
            source="test",
            status="confirmed",
        )
    )
    process.experience("stone", "你好", object_ref="lux")
    segments = process.activity_ledger.list_experiences("stone")
    inbound = next(
        item for item in segments if item.output_kind is OutputKind.EXTERNAL_INPUT
    )
    reply = next(
        item for item in segments if item.output_kind is OutputKind.SUBJECT_REPLY
    )
    expected = {"lux": "OBJ-LUX", "路光": "OBJ-LUX"}
    assert inbound.objects == expected
    assert reply.objects == expected
    assert inbound.mentioned_object_ids == ("OBJ-LUX",)
    assert reply.mentioned_object_ids == ("OBJ-LUX",)
    fact = next(
        item
        for item in repository.list_history("stone", HistoryKind.FACT)
        if item.event_type == "external_input"
    )
    assert fact.content["objects"] == expected


def test_experience_keeps_third_party_and_overwrites_speaker_name(tmp_path):
    process, _repository = runtime(tmp_path)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-A",
            label="小明",
            aliases=("阿明",),
            source="test",
            status="confirmed",
        )
    )
    process.experience(
        "stone",
        "你好",
        object_ref="小明",
        objects={"小明": "OBJ-WRONG", "贾母": "OBJ-JIA"},
    )
    inbound = process.activity_ledger.list_experiences("stone")[0]
    assert inbound.objects["小明"] == "OBJ-A"
    assert inbound.objects["阿明"] == "OBJ-A"
    assert inbound.objects["贾母"] == "OBJ-JIA"


def test_wait_state_segment_carries_objects(tmp_path):
    process, _repository = runtime(tmp_path, WaitModel())
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER",
            label="user",
            source="test",
            status="confirmed",
        )
    )
    process.experience("stone", "先说到这里", object_ref="user")
    state = next(
        item
        for item in process.activity_ledger.list_experiences("stone")
        if item.output_kind is OutputKind.SUBJECT_STATE
    )
    assert state.objects == {"user": "OBJ-USER"}
    assert "OBJ-USER" in state.mentioned_object_ids


def test_build_batch_fills_reply_interlocutor_from_objects():
    ledger = InProcessExperienceLedger()
    inbound = ledger.append_external(
        "stone",
        actor_object_id="OBJ-LUX",
        text_raw="你好",
        objects={"lux": "OBJ-LUX"},
        mentioned_object_ids=("OBJ-LUX",),
    )
    reply = ledger.append_subject_reply(
        "stone",
        text_raw="回应",
        objects={"lux": "OBJ-LUX"},
        mentioned_object_ids=("OBJ-LUX",),
    )
    control = InProcessMemoryControl(ledger, _NoopMemory(), flush_max_segments=2)
    batch = control.build_batch("stone")
    assert batch is not None
    by_id = {item.segment_id: item for item in batch.experiences}
    assert by_id[inbound.segment_id].interlocutor == "OBJ-LUX"
    assert by_id[reply.segment_id].interlocutor == "OBJ-LUX"
    assert by_id[inbound.segment_id].text == "lux：你好"
    assert by_id[reply.segment_id].text == "匠石：回应"
    assert reply.actor_object_id is None


def test_build_batch_does_not_pick_random_peer_when_objects_have_many():
    ledger = InProcessExperienceLedger()
    reply = ledger.append_subject_reply(
        "stone",
        text_raw="回应",
        objects={"lux": "OBJ-LUX", "贾母": "OBJ-JIA"},
        mentioned_object_ids=("OBJ-LUX",),
    )
    ambiguous = ledger.append_subject_reply(
        "stone",
        text_raw="另一句",
        objects={"lux": "OBJ-LUX", "贾母": "OBJ-JIA"},
    )
    control = InProcessMemoryControl(ledger, _NoopMemory(), flush_max_segments=2)
    batch = control.build_batch("stone")
    assert batch is not None
    by_id = {item.segment_id: item for item in batch.experiences}
    assert by_id[reply.segment_id].interlocutor == "OBJ-LUX"
    assert by_id[ambiguous.segment_id].interlocutor is None


def test_build_batch_dialogue_keeps_speech_and_action():
    ledger = InProcessExperienceLedger()
    inbound = ledger.append_external(
        "stone",
        actor_object_id="OBJ-LUX",
        text_raw="你会游泳吗",
        objects={"lux": "OBJ-LUX", "贾母": "OBJ-JIA"},
        mentioned_object_ids=("OBJ-LUX",),
    )
    reply = ledger.append_subject_reply(
        "stone",
        text_raw="我是一段程序，没有身体。（动作：抬起手比划了两下）",
        objects={"lux": "OBJ-LUX", "贾母": "OBJ-JIA"},
        mentioned_object_ids=("OBJ-LUX",),
    )
    control = InProcessMemoryControl(ledger, _NoopMemory(), flush_max_segments=2)
    batch = control.build_batch("stone")
    assert batch is not None
    by_id = {item.segment_id: item for item in batch.experiences}
    assert by_id[inbound.segment_id].text == "lux：你会游泳吗"
    assert by_id[reply.segment_id].text == (
        "匠石：我是一段程序，没有身体。（动作：抬起手比划了两下）"
    )
    assert "贾母：" not in by_id[inbound.segment_id].text


def test_build_batch_does_not_double_speaker_prefix():
    ledger = InProcessExperienceLedger()
    inbound = ledger.append_external(
        "stone",
        actor_object_id="OBJ-LUX",
        text_raw="lux：你会游泳吗",
        objects={"lux": "OBJ-LUX"},
    )
    control = InProcessMemoryControl(ledger, _NoopMemory(), flush_max_segments=1)
    batch = control.build_batch("stone")
    assert batch is not None
    assert batch.experiences[0].segment_id == inbound.segment_id
    assert batch.experiences[0].text == "lux：你会游泳吗"


def test_build_batch_embodied_only_becomes_action_line():
    ledger = InProcessExperienceLedger()
    state = ledger.append_subject_state(
        "stone",
        state_delta={"mode": "respond", "embodied": ["抬起手比划了两下"]},
        objects={"lux": "OBJ-LUX"},
        mentioned_object_ids=("OBJ-LUX",),
    )
    control = InProcessMemoryControl(ledger, _NoopMemory(), flush_max_segments=1)
    batch = control.build_batch("stone")
    assert batch is not None
    assert batch.experiences[0].segment_id == state.segment_id
    assert batch.experiences[0].text == "匠石：（动作：抬起手比划了两下）"


def test_build_batch_think_state_is_not_dialogue():
    ledger = InProcessExperienceLedger()
    ledger.append_subject_state(
        "stone",
        state_delta={"mode": "think", "reason": "先想想"},
        objects={"lux": "OBJ-LUX"},
    )
    control = InProcessMemoryControl(ledger, _NoopMemory(), flush_max_segments=1)
    batch = control.build_batch("stone")
    assert batch is not None
    assert batch.experiences[0].text == ""
    assert "think" not in batch.experiences[0].text
    assert "{" not in batch.experiences[0].text


def test_build_batch_uses_identity_display_name():
    ledger = InProcessExperienceLedger()
    ledger.append_subject_reply("stone", text_raw="我在。")
    control = InProcessMemoryControl(
        ledger,
        _NoopMemory(),
        flush_max_segments=1,
        subject_name=lambda _sid: "石头人",
    )
    batch = control.build_batch("stone")
    assert batch is not None
    assert batch.experiences[0].text == "石头人：我在。"
