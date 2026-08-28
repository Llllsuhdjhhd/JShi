"""skill 框架测试：结构化解析、降级、复用、登记表。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Mapping

import pytest

from jshi.core import Provenance, SubjectState
from jshi.models import ModelPort, ModelRequest, ModelResponse, ModelSpeaker, ResponseItem
from jshi.skill import (
    CognitionSkill,
    Skill,
    SkillError,
    SkillModelPort,
    SkillRegistry,
    parse_json_object,
)


class FakeModel(ModelPort):
    """受控模型：返回给定 payload 的 JSON，或（payload=None）一行非 JSON 文本。"""

    name = "deepseek-flash"

    def __init__(self, payload=None) -> None:
        self.payload = payload

    def generate(self, request: ModelRequest) -> ModelResponse:
        if self.payload is None:
            return ModelResponse(text="不是JSON的一行话", model=self.name)
        return ModelResponse(
            text=json.dumps(self.payload, ensure_ascii=False), model=self.name
        )


def make_request() -> ModelRequest:
    subject_state = SubjectState(
        subject_id="stone",
        identity_summary="我是匠石。",
        current_stance="诚实。",
        salient_values=("诚实",),
        commitments=(),
        provenance=Provenance(source="test"),
    )
    return ModelRequest(
        purpose="subject_activity",
        input_text="dp: 这次按这个口径定。",
        subject_state=subject_state,
        speaker=ModelSpeaker(
            object_id="OBJ-DP", label="dp", aliases=("D",), status="confirmed"
        ),
        context=({"id": "seg-12", "kind": "experience", "content": "…", "status": "active", "source": "activity"},),
    )


def _payload() -> dict:
    return {
        "response_plan": {
            "mode": "respond",
            "reason": "确认口径",
            "items": [{"channel": "verbal", "text": "对，working_set_limit=窗口x1/66。"}],
        },
        "context_assessment": {"remove": [], "drop_recall": [], "focus": ["seg-12"]},
        "object_assessment": {"conclusion": "confirm", "object_id": "OBJ-DP", "label": "dp", "reason": "档案一致"},
        "recall_requests": [{"query": "魔法书 参数", "budget": 3, "level": 2, "object_ids": ["OBJ-DP"], "anchor_event_ids": []}],
        "importance_ranking": [{"id": "seg-12", "importance": 0.9, "reason": "当前焦点"}],
    }


def test_cognition_skill_produces_usage_segments(tmp_path):
    skill = CognitionSkill(FakeModel(_payload()))
    resp = skill.run(make_request())
    assert resp.model == "deepseek-flash@v1"  # I-004：模型 + skill 版本
    assert resp.response_plan.mode == "respond"
    assert "working_set_limit" in resp.response_plan.verbal_text()
    assert resp.context_assessment.focus == ("seg-12",)
    assert resp.object_assessment.conclusion == "confirm"
    assert resp.object_assessment.object_id == "OBJ-DP"
    assert resp.recall_requests[0].level == 2
    # 第 5 用途段"重要性排序"（给 14，占位）
    assert resp.importance_ranking[0].id == "seg-12"
    assert resp.importance_ranking[0].importance == 0.9


def test_system_extra_renders_speaker_placeholders():
    skill = CognitionSkill(FakeModel(_payload()))

    system = skill.system_extra(make_request())

    assert "{speaker_label}" not in system
    assert "{speaker_object_id}" not in system
    assert "dp" in system
    assert "OBJ-DP" in system


def test_skill_framework_is_reused_by_other_skills():
    class ObjectConfirmSkill(Skill[dict]):
        name = "object_confirm"
        instruction = "判断说话人对象身份。"
        schema = {
            "type": "object",
            "properties": {
                "conclusion": {"enum": ["confirm", "deny", "uncertain"]},
                "object_id": {"type": "string"},
            },
        }

        def parse(self, data: Mapping[str, Any]) -> dict:
            return {
                "conclusion": data.get("conclusion"),
                "object_id": data.get("object_id"),
            }

    oc = ObjectConfirmSkill(FakeModel({"conclusion": "confirm", "object_id": "OBJ-DP"}))
    result = oc.run(make_request())
    assert result == {"conclusion": "confirm", "object_id": "OBJ-DP"}
    assert oc.model_tag == "deepseek-flash@v1"


def test_skill_registry_register_and_get():
    registry = SkillRegistry()
    registry.register(CognitionSkill(FakeModel(_payload())))
    class Fake(Skill[dict]):
        name = "fake"
        schema = {}
        instruction = ""
        def parse(self, data):
            return dict(data)
    registry.register(Fake(FakeModel()))
    assert registry.names() == ("cognition", "fake")
    assert registry.get("fake") is not None
    assert registry.get("missing") is None


def test_cognition_skill_degrades_on_non_json():
    skill = CognitionSkill(FakeModel(None))
    resp = skill.run(make_request())
    assert resp.response_plan.mode == "think"
    assert resp.response_plan.verbal_text() == ""
    assert resp.response_plan.items == ()
    assert resp.metadata is not None
    assert resp.metadata["skill_fallback"] == "non_json"
    assert "不是JSON" in str(resp.metadata.get("raw_preview", ""))


def test_invalid_mode_becomes_think_and_keeps_patch():
    payload = dict(_payload())
    payload["response_plan"] = {
        "mode": "chat",
        "reason": "bad",
        "items": [{"channel": "verbal", "text": "不该说出口"}],
    }
    resp = CognitionSkill(FakeModel(payload)).run(make_request())
    assert resp.response_plan.mode == "think"
    assert resp.response_plan.verbal_text() == ""
    assert resp.context_assessment.focus == ("seg-12",)


def test_silent_mode_drops_verbal_keeps_embodied():
    payload = dict(_payload())
    payload["response_plan"] = {
        "mode": "wait",
        "reason": "等对方下一条",
        "items": [
            {"channel": "verbal", "text": "不该说"},
            {"channel": "embodied", "text": "保持等待"},
        ],
    }
    resp = CognitionSkill(FakeModel(payload)).run(make_request())
    assert resp.response_plan.mode == "wait"
    assert resp.response_plan.verbal_text() == ""
    assert resp.response_plan.items == (
        ResponseItem(channel="embodied", text="保持等待"),
    )


def test_context_view_fragment_id_is_not_a_segment_ref():
    payload = dict(_payload())
    payload["context_assessment"] = {
        "remove": ["context-v3", "seg-12"],
        "drop_recall": [],
        "focus": ["context-v3"],
    }
    resp = CognitionSkill(FakeModel(payload)).run(make_request())
    assert resp.context_assessment.remove == ("seg-12",)
    assert resp.context_assessment.focus == ()


def test_recall_budget_and_level_are_clamped():
    payload = dict(_payload())
    payload["recall_requests"] = [
        {
            "query": "魔法书",
            "budget": 99,
            "level": 20,
            "object_ids": [],
            "anchor_event_ids": [],
        }
    ]
    resp = CognitionSkill(FakeModel(payload)).run(make_request())

    assert resp.recall_requests[0].budget == 3
    assert resp.recall_requests[0].level == 9


def test_skill_model_port_routes_subject_activity_but_not_reflection():
    skill = CognitionSkill(FakeModel(_payload()))
    port = SkillModelPort(skill)

    subject = port.generate(make_request())
    assert subject.model == "deepseek-flash@v1"
    assert subject.response_plan.mode == "respond"
    assert subject.context_assessment.focus == ("seg-12",)

    reflection = port.generate(replace(make_request(), purpose="reflection"))
    assert reflection.response_plan.verbal_text() == json.dumps(
        _payload(), ensure_ascii=False
    )


def test_parse_json_object_requires_object():
    with pytest.raises(SkillError):
        parse_json_object("123")
    with pytest.raises(SkillError):
        parse_json_object("not json")
