"""尚未投递的原话留在账本和片场里，不再单独贴进认知提示词。"""

from __future__ import annotations

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.models.prompt import build_user
from jshi.recognition import ObjectProfile
from jshi.style import SMITH
from jshi.subject import SubjectProcess, SubjectRepository


class CaptureModel:
    name = "capture"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(text="好的。", model=self.name, rewritten_context="现场")


def _process(tmp_path, model) -> SubjectProcess:
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, model)
    process.profiles.create(
        ObjectProfile(object_id="OBJ-USER", label="user", source="test", status="confirmed")
    )
    return process


def _cognition_requests(model: CaptureModel) -> list[ModelRequest]:
    return [item for item in model.requests if item.purpose == "subject_activity"]


def test_unflushed_talk_stays_in_the_ledger_not_a_prompt_block(tmp_path):
    model = CaptureModel()
    process = _process(tmp_path, model)
    process.experience("stone", "第一句", object_ref="user")
    process.experience("stone", "第二句", object_ref="user")

    second = _cognition_requests(model)[-1]
    assert second.input_text == "第二句"
    text = build_user(second)
    assert "尚未存入记忆" not in text
    assert "第二句" in text
    raw = [segment.text_raw for segment in process.activity_ledger.list_experiences("stone")]
    assert "第一句" in raw
    assert "第二句" in raw


def test_persona_prompt_uses_the_scene_not_an_unflushed_dump(tmp_path):
    model = CaptureModel()
    process = _process(tmp_path, model)
    process.style_packs.set("stone", SMITH)
    process.zone_store.boot("stone", scene=["user说：“第一句”", "我说：好的。"])
    process.experience("stone", "第二句", object_ref="user")

    seen = _cognition_requests(model)[-1]
    text = (seen.persona_user_text or "").strip()
    assert text
    assert "尚未存入记忆" not in text
    assert "【此时的片场】" in text
    assert "第一句" in text
    assert "第二句" in text
    raw = [segment.text_raw for segment in process.activity_ledger.list_experiences("stone")]
    assert "第二句" in raw


def test_scene_budget_is_longer_than_the_flush_threshold():
    from jshi.core.params import memory_flush_chars, suxipo_zone_chars

    flush = memory_flush_chars()
    scene = suxipo_zone_chars()
    assert flush == 3000
    assert scene == 5000
    assert 1.5 * flush <= scene <= 2 * flush
