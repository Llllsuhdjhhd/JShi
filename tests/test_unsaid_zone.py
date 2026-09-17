"""I-021：unsaid 解析进 ResponsePlan，人格片场程序追加「（未开口）…」。"""

from __future__ import annotations

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import (
    ModelPort,
    ModelRequest,
    ModelResponse,
    ResponseItem,
    ResponsePlan,
    format_unsaid_zone_block,
)
from jshi.recognition import ObjectProfile
from jshi.skill.cognition import CognitionSkill, _persona_to_model_response
from jshi.style import SMITH, StylePackStore, ZoneStore
from jshi.subject import SubjectProcess, SubjectRepository


def test_format_unsaid_zone_block_adds_prefix_once():
    assert format_unsaid_zone_block("天气已到，暂缓") == "（未开口）天气已到，暂缓"
    assert format_unsaid_zone_block("（未开口）已有前缀") == "（未开口）已有前缀"
    assert format_unsaid_zone_block("  ") == ""
    assert (
        format_unsaid_zone_block("火星人这次像是报新料，未必又在考我")
        == "（未开口）火星人这次像是报新料，未必又在考我"
    )


def test_unsaid_prompt_asks_to_name_speaker_in_prose():
    from jshi.skill.cognition import CognitionSkill
    from jshi.style import SMITH, instruction_for, reply_instruction_for, write_instruction_for

    reply = reply_instruction_for(SMITH)
    write = write_instruction_for(SMITH)
    wood = CognitionSkill(FakeModel({})).instruction
    packed = instruction_for(SMITH)
    for text in (reply, write, wood, packed):
        assert "点名当前说话人" in text
        assert "不要另加" in text or "也不要改成「（名字）」标签" in text
    assert "明确事项" in reply
    assert "【来源谁优先】" in reply
    assert "【来源谁优先】" in wood


def test_persona_parse_keeps_unsaid_on_respond():
    resp = _persona_to_model_response(
        {
            "mode": "respond",
            "reply": "先聊眼前这事。",
            "action": "微微点头",
            "unsaid": "天气结果已到，暂缓告知",
            "reason": "对方在谈别的",
        },
        model="t",
    )
    assert resp.response_plan.mode == "respond"
    assert resp.response_plan.verbal_text() == "先聊眼前这事。"
    assert resp.response_plan.unsaid_text() == "天气结果已到，暂缓告知"


def test_wood_parse_reads_response_plan_unsaid():
    payload = {
        "response_plan": {
            "mode": "think",
            "reason": "没人点到我",
            "unsaid": "工具结果已到，等空再说",
            "items": [],
        }
    }
    resp = CognitionSkill(FakeModel({})).parse(payload)
    assert resp.response_plan.mode == "think"
    assert resp.response_plan.unsaid_text() == "工具结果已到，等空再说"
    assert resp.response_plan.verbal_text() == ""


class FakeModel(ModelPort):
    name = "fake"

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(model=self.name, text="{}")


class ReplyWithUnsaid(ModelPort):
    name = "reply-unsaid"

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(
            model=self.name,
            response_plan=ResponsePlan(
                mode="respond",
                reason="接住",
                unsaid="结果已到，暂缓告知",
                items=(
                    ResponseItem(channel="verbal", text="嗯，我听着。"),
                    ResponseItem(channel="embodied", text="微微点头"),
                ),
            ),
        )


class EmptyWrite(ModelPort):
    name = "empty-write"

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(model=self.name, zone_edit=())


def test_persona_zone_appends_unsaid_block(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    zone = ZoneStore(tmp_path / "zone.json")
    zone.boot("stone", value="我是匠石。", scene=("先前在场。",))
    process = SubjectProcess(
        repository,
        identities,
        ReplyWithUnsaid(),
        write_zone=EmptyWrite(),
        style_packs=StylePackStore(tmp_path / "style.json"),
        zone_store=zone,
    )
    process.style_packs.set("stone", SMITH)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-U", label="lux", source="test", status="confirmed"
        )
    )
    process.experience("stone", "先说别的", object_ref="lux")
    rendered = process.zone_store.render("stone")
    assert "我说：“嗯，我听着。”" in rendered
    assert "（未开口）结果已到，暂缓告知" in rendered
    assert "lux说：" in rendered
