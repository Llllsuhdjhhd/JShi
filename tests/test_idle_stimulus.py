"""闲时刺激：不冒充对方原话；阿丘人格；调试计数封顶。"""

from __future__ import annotations

import time

from jshi.app.talk_session import IdleTrial
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import (
    ModelRequest,
    ModelResponse,
    STIMULUS_IDLE,
    format_turn_input,
)
from jshi.recognition import ObjectProfile
from jshi.style import AQIU, SMITH, instruction_for, reply_instruction_for
from jshi.subject import SubjectProcess, SubjectRepository
from jshi.experienceledger import ActorKind, OutputKind


class CaptureModel:
    name = "capture"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            text="好。",
            model=self.name,
            rewritten_context="现场",
        )


def _process(tmp_path, model=None):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, model or CaptureModel())
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="user", source="test", status="confirmed"
        )
    )
    return process, repository


def test_format_turn_input_idle_is_not_speaker_colon():
    speech = format_turn_input("路光", "你好")
    idle = format_turn_input("路光", "", stimulus=STIMULUS_IDLE)
    assert speech == "路光：你好"
    assert idle == "和上次路光说话又过了3分钟"
    assert "路光：" not in idle


def test_aqiu_copies_smith_without_config_names():
    assert "阿丘" not in instruction_for(AQIU)
    assert "斯密斯" not in instruction_for(AQIU)
    assert "阿丘" not in reply_instruction_for(AQIU)
    reply = reply_instruction_for(AQIU)
    assert "和上次" in reply
    assert "又过了" in reply
    assert "不要当成对方刚说了这句" in reply
    assert "和上次" not in reply_instruction_for(SMITH)


def test_idle_trial_stops_after_two_rounds():
    trial = IdleTrial(seconds=0.01, max_rounds=2, enabled=True)
    trial.last = time.monotonic() - 1
    assert trial.due() is True
    trial.note_fire()
    trial.last = time.monotonic() - 1
    assert trial.can_fire() is True
    trial.note_fire()
    assert trial.can_fire() is False
    assert trial.due() is False


def test_idle_experience_does_not_append_external(tmp_path):
    model = CaptureModel()
    process, repository = _process(tmp_path, model)
    result = process.experience(
        "stone", "ignored", object_ref="user", stimulus=STIMULUS_IDLE
    )
    idle_facts = [
        item
        for item in repository.list_history("stone")
        if item.event_type == "idle_stimulus"
    ]
    assert idle_facts
    assert not any(item.event_type == "external_input" for item in repository.list_history("stone"))
    segments = process.activity_ledger.list_experiences("stone")
    assert not any(
        item.actor_kind is ActorKind.EXTERNAL
        and item.output_kind is OutputKind.EXTERNAL_INPUT
        for item in segments
    )
    user = model.requests[0]
    assert user.stimulus == STIMULUS_IDLE
    from jshi.models.prompt import build_user

    text = build_user(user)
    assert "user：" not in text
    assert "和上次user说话又过了3分钟" in text
    assert result.current_state.stimulus == STIMULUS_IDLE
    assert result.current_state.input_text == ""


def test_idle_does_not_append_speaker_said_to_zone(tmp_path):
    from jshi.style import StylePackStore, ZoneStore, SMITH

    model = CaptureModel()
    process, _repository = _process(tmp_path, model)
    process.style_packs = StylePackStore(tmp_path / "style_pack.json")
    process.zone_store = ZoneStore(tmp_path / "zone.json")
    process.style_packs.set("stone", SMITH)
    process.zone_store.boot("stone", value="我是匠石。", scene=["先前在场。"])
    process.experience("stone", "", object_ref="user", stimulus=STIMULUS_IDLE)
    scene = "\n".join(process.zone_store.get("stone"))
    assert "user说" not in scene
    assert "先前在场。" in scene
