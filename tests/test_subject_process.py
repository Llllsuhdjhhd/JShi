from __future__ import annotations

import pytest

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.recognition import ObjectProfile
from jshi.subject import (
    ActivityStatus,
    EpistemicStatus,
    HistoryKind,
    PersonalKind,
    PersonalStatus,
    SubjectProcess,
    SubjectRepository,
)
from tests.value_seed import accepted_value, import_values


class ContextModel:
    name = "context-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        personal = "|".join(str(item.get("content", "")) for item in request.context)
        return ModelResponse(
            text=f"{request.purpose}:{request.input_text}:个人世界={personal}",
            model=self.name,
        )


def runtime(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, ContextModel())
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="user", source="test", status="confirmed"
        )
    )
    return process, repository


def test_external_activity_records_fact_and_subject_histories(tmp_path):
    process, repository = runtime(tmp_path)

    result = process.experience("stone", "我今天有些疲倦", object_ref="user")

    assert result.activity.status is ActivityStatus.COMPLETED
    assert result.response_plan.mode == "respond"
    assert result.action_text
    assert result.current_state.active_segment_ids == ()
    facts = repository.list_history("stone", HistoryKind.FACT)
    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    assert [item.event_type for item in facts] == [
        "external_input",
        "language_action",
    ]
    types = [item.event_type for item in subject]
    for required in (
        "context_window_loaded",
        "current_state_assembled",
        "activity_created",
        "activity_response_state",
        "activity_completed",
    ):
        assert required in types
    assert "cognitive_content_appeared" not in types
    timing = result.timing
    assert timing is not None
    names = [name for name, _ms in timing.steps]
    assert "①落位" in names
    assert "⑤认知" in names
    assert "⑦收尾" in names
    assert timing.total_ms >= 0
    assert process.last_activity_timing == timing


def test_assemble_loads_existing_commitments_only(tmp_path):
    process, repository = runtime(tmp_path)
    commitment = process.add_personal_item(
        "stone",
        PersonalKind.COMMITMENT,
        "下次继续询问他的近况",
    )
    import_values(process, "stone", [accepted_value("优先坦率表达")])

    assembled = process.assemble_current_state("stone", "今天先聊到这里")

    assert commitment.content in assembled.subject_state.commitments
    assert "优先坦率表达" in assembled.subject_state.salient_values
    # Assembly must not create new personal items.
    assert len(repository.list_personal_items("stone")) == 1


def test_epistemic_transition_preserves_revision_history(tmp_path):
    process, repository = runtime(tmp_path)
    process.experience("stone", "也许明天会更好", object_ref="user")
    reflection = process.reflect("stone", "回顾刚才的理解")

    provisional = process.transition_cognition(
        reflection.id,
        EpistemicStatus.PROVISIONAL,
        "目前证据有限，先暂时接受",
    )
    revised = process.transition_cognition(
        reflection.id,
        EpistemicStatus.REVISED,
        "新事实改变了原来的理解",
    )

    assert provisional.epistemic_status is EpistemicStatus.PROVISIONAL
    assert revised.epistemic_status is EpistemicStatus.REVISED
    transitions = repository.list_transitions(reflection.id)
    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("considering", "provisional"),
        ("provisional", "revised"),
    ]
    with pytest.raises(ValueError):
        process.transition_cognition(
            reflection.id, EpistemicStatus.ACCEPTED, "不能从已修正状态恢复"
        )


def test_commitment_persists_and_closes_explicitly(tmp_path):
    process, repository = runtime(tmp_path)
    commitment = process.add_personal_item(
        "stone", PersonalKind.COMMITMENT, "下次继续询问他的近况"
    )

    result = process.experience("stone", "今天先聊到这里", object_ref="user")
    assert commitment.content in result.action_text

    closed = process.close_personal_item(
        commitment.id, PersonalStatus.COMPLETED, "已经在后续交流中履行"
    )
    assert closed.status is PersonalStatus.COMPLETED
    assert repository.list_personal_items("stone", PersonalKind.COMMITMENT) == ()
    assert repository.list_transitions(commitment.id)[0].reason == "已经在后续交流中履行"


def test_same_cognition_forms_different_outputs_from_personal_world(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("a", "甲", "相同基础型"))
    identities.create(IdentityProfile("b", "乙", "相同基础型"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, ContextModel())
    import_values(process, "a", [accepted_value("优先坦率表达")])
    import_values(process, "b", [accepted_value("优先温和表达")])

    result_a = process.experience("a", "怎样指出朋友的错误？", object_ref="user")
    result_b = process.experience("b", "怎样指出朋友的错误？", object_ref="user")

    assert "优先坦率表达" in result_a.action_text
    assert "优先温和表达" in result_b.action_text
    assert result_a.action_text != result_b.action_text


def test_reflection_is_internal_cognition_not_external_fact(tmp_path):
    process, repository = runtime(tmp_path)
    process.experience("stone", "今天发生了一件重要的事", object_ref="user")

    reflection = process.reflect("stone", "回顾我今天形成了什么理解")

    assert reflection.epistemic_status is EpistemicStatus.CONSIDERING
    assert repository.get_activity(reflection.activity_id).status is ActivityStatus.COMPLETED
    subject_history = repository.list_history("stone", HistoryKind.SUBJECT)
    reflections = [
        item for item in subject_history if item.event_type == "reflection"
    ]
    assert reflections
    assert reflections[-1].content["cognitive_content_id"] == reflection.id


