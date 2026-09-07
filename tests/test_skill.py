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
    WriteZoneSkill,
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
        "rewritten_context": "对方定口径。承诺仍在。",
    }


def test_cognition_skill_produces_usage_segments(tmp_path):
    skill = CognitionSkill(FakeModel(_payload()))
    resp = skill.run(make_request())
    assert resp.model == "deepseek-flash@v10"  # I-004：模型 + skill 版本
    assert resp.response_plan.mode == "respond"
    assert "working_set_limit" in resp.response_plan.verbal_text()
    assert resp.context_assessment.focus == ("seg-12",)
    assert resp.object_assessment.conclusion == "confirm"
    # 回复调用不再产出写场（写场已独立到 WriteZoneSkill）。
    assert resp.rewritten_context == ""
    assert resp.object_assessment.object_id == "OBJ-DP"
    assert resp.recall_requests[0].level == 2
    # 第 5 用途段"重要性排序"（给 14，占位）
    assert resp.importance_ranking[0].id == "seg-12"
    assert resp.importance_ranking[0].importance == 0.9


def test_system_extra_has_compact_schema_and_no_speaker():
    skill = CognitionSkill(FakeModel(_payload()))
    system = skill.system_extra(make_request())
    assert "{speaker_label}" not in system
    assert "OBJ-DP" not in system
    assert "\n  " not in system.split("JSON Schema：", 1)[1]
    assert '"enum":[' in system


def test_instruction_keeps_rules_without_examples():
    text = CognitionSkill.instruction
    assert "【关于你】" in text
    assert "【关于输入】" in text
    assert "【价值】" in text
    assert "【回应方式】" in text
    assert "【对象确认】" in text
    assert "【输出格式】" in text
    assert "{values}" in text
    assert "{style_instruction}" in text
    assert "{speaker_label}" not in text
    assert "不伤害人类" in text
    assert "respond（回话）" in text
    assert "ignore（忽略）" in text
    # 回复调用不再写场；写场在 WriteZoneSkill。
    assert "rewritten_context" not in text
    assert "才提出 recall_requests" not in text
    assert "追加评价与召回" not in text
    assert "对象确认" in text
    assert "【正例】" not in text
    assert "【反例】" not in text
    assert "【本轮材料】" not in text
    assert "【如何选择对外姿态】" not in text


def test_empty_object_assessment_id_is_filled_from_speaker():
    payload = dict(_payload())
    payload["object_assessment"] = {
        "conclusion": "confirm",
        "object_id": "",
        "label": "",
        "reason": "渠道已绑定",
    }
    resp = CognitionSkill(FakeModel(payload)).run(make_request())
    assert resp.object_assessment.object_id == "OBJ-DP"
    assert resp.object_assessment.label == "dp"


def test_object_assessment_name_is_resolved_to_speaker_id():
    payload = dict(_payload())
    payload["object_assessment"] = {
        "conclusion": "confirm",
        "object_id": "dp",
        "label": "",
        "reason": "渠道已绑定",
    }
    resp = CognitionSkill(FakeModel(payload)).run(make_request())
    assert resp.object_assessment.object_id == "OBJ-DP"


def test_rewrite_replaces_patch_in_instruction():
    # 回复调用(①)：system 不再含写场契约。
    reply_system = CognitionSkill(FakeModel(_payload())).system_extra(make_request())
    assert "【现场】" not in reply_system
    assert "rewritten_context" not in reply_system
    assert "才提出 recall_requests" not in reply_system
    assert "追加评价与召回" not in reply_system
    # 写场调用(②)：WriteZoneSkill 给出整份重写指令。
    write_instruction = WriteZoneSkill.instruction
    assert "rewritten_context" in write_instruction


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


def test_cognition_skill_salvages_prose_as_verbal_on_non_json():
    skill = CognitionSkill(FakeModel(None))
    resp = skill.run(make_request())
    assert resp.response_plan.mode == "respond"
    assert resp.response_plan.verbal_text() == "不是JSON的一行话"
    assert resp.metadata is not None
    assert resp.metadata["skill_fallback"] == "prose_as_verbal"
    assert "不是JSON" in str(resp.metadata.get("raw_preview", ""))


def test_cognition_skill_thinks_on_broken_json_not_silently():
    class BrokenJsonModel(ModelPort):
        name = "broken-json"

        def generate(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(text='{"response_plan": ', model=self.name)

    skill = CognitionSkill(BrokenJsonModel())
    resp = skill.run(make_request())
    assert resp.response_plan.mode == "think"
    assert resp.response_plan.verbal_text() == ""
    assert resp.metadata["skill_fallback"] == "non_json"


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


def test_memory_ratings_are_parsed():
    payload = dict(_payload())
    payload["memory_ratings"] = {
        "items": [
            {
                "ref": "M1",
                "relevance": "related",
                "helps_understanding": 2,
                "used_in_reply": "Relied",
                "misleading": False,
                "redundant": False,
                "object_fit": "match",
            }
        ],
        "coverage": "thin",
        "gap_query": "lux 岭南",
    }
    resp = CognitionSkill(FakeModel(payload)).run(make_request())
    assert resp.memory_ratings.coverage == "thin"
    assert resp.memory_ratings.gap_query == "lux 岭南"
    assert resp.memory_ratings.items[0].ref == "M1"
    assert resp.memory_ratings.items[0].used_in_reply == "relied"
    assert resp.memory_ratings.items[0].helps_understanding == 2


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
    assert subject.model == "deepseek-flash@v10"
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


def test_parse_json_object_tolerates_markdown_fence():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_object_extracts_first_object_with_preamble():
    assert parse_json_object('好的，结果如下：\n{"a": 1}\n') == {"a": 1}
