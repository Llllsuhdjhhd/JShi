from __future__ import annotations

import pytest

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.subject import (
    ActivityStatus,
    EpistemicStatus,
    HistoryKind,
    PersonalKind,
    PersonalStatus,
    SubjectProcess,
    SubjectRepository,
)


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
    return SubjectProcess(repository, identities, ContextModel()), repository


def test_external_activity_records_fact_and_subject_histories(tmp_path):
    process, repository = runtime(tmp_path)

    result = process.experience("stone", "我今天有些疲倦")

    assert result.activity.status is ActivityStatus.COMPLETED
    assert result.thought.epistemic_status is EpistemicStatus.CONSIDERING
    assert result.thought.model == "context-model"
    assert result.current_state.open_matter_ids == ()
    facts = repository.list_history("stone", HistoryKind.FACT)
    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    assert [item.event_type for item in facts] == [
        "external_input",
        "language_action",
    ]
    assert [item.event_type for item in subject] == [
        "input_attributed",
        "current_state_assembled",
        "cognitive_content_appeared",
    ]


def test_assemble_loads_existing_open_matter_only(tmp_path):
    process, repository = runtime(tmp_path)
    open_matter = process.propose_open_matter(
        "stone",
        "继续理解朋友最近的疲倦",
        source_ids=("seed-source",),
    )

    assembled = process.assemble_current_state("stone", "今天先聊到这里")

    assert open_matter.id in assembled.open_matter_ids
    assert "继续理解朋友最近的疲倦" in assembled.subject_state.concerns
    # Assembly must not create new personal items.
    assert len(repository.list_personal_items("stone", PersonalKind.CONCERN)) == 1


def test_propose_open_matter_requires_sources(tmp_path):
    process, _repository = runtime(tmp_path)
    with pytest.raises(ValueError, match="source_ids"):
        process.propose_open_matter("stone", "无来源的跟进", source_ids=())


def test_epistemic_transition_preserves_revision_history(tmp_path):
    process, repository = runtime(tmp_path)
    result = process.experience("stone", "也许明天会更好")

    provisional = process.transition_cognition(
        result.thought.id,
        EpistemicStatus.PROVISIONAL,
        "目前证据有限，先暂时接受",
    )
    revised = process.transition_cognition(
        result.thought.id,
        EpistemicStatus.REVISED,
        "新事实改变了原来的理解",
    )

    assert provisional.epistemic_status is EpistemicStatus.PROVISIONAL
    assert revised.epistemic_status is EpistemicStatus.REVISED
    transitions = repository.list_transitions(result.thought.id)
    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("considering", "provisional"),
        ("provisional", "revised"),
    ]
    with pytest.raises(ValueError):
        process.transition_cognition(
            result.thought.id, EpistemicStatus.ACCEPTED, "不能从已修正状态恢复"
        )


def test_open_matter_and_commitment_persist_and_close_explicitly(tmp_path):
    process, repository = runtime(tmp_path)
    concern = process.propose_open_matter(
        "stone",
        "继续理解朋友最近的疲倦",
        source_ids=("manual-seed",),
    )
    commitment = process.add_personal_item(
        "stone", PersonalKind.COMMITMENT, "下次继续询问他的近况"
    )

    result = process.experience("stone", "今天先聊到这里")
    assert concern.id in result.activity.active_concern_ids
    assert commitment.content in result.action_text

    closed_open = process.close_personal_item(
        concern.id, PersonalStatus.RELEASED, "暂时放下，改日再问"
    )
    closed = process.close_personal_item(
        commitment.id, PersonalStatus.COMPLETED, "已经在后续交流中履行"
    )
    assert closed_open.status is PersonalStatus.RELEASED
    assert closed.status is PersonalStatus.COMPLETED
    assert repository.list_personal_items("stone", PersonalKind.COMMITMENT) == ()
    assert repository.list_personal_items("stone", PersonalKind.CONCERN) == ()
    assert repository.list_transitions(commitment.id)[0].reason == "已经在后续交流中履行"

    later = process.assemble_current_state("stone", "又见面了")
    assert later.open_matter_ids == ()


def test_same_cognition_forms_different_outputs_from_personal_world(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("a", "甲", "相同基础型"))
    identities.create(IdentityProfile("b", "乙", "相同基础型"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, ContextModel())
    process.add_personal_item("a", PersonalKind.VALUE, "优先坦率表达")
    process.add_personal_item("b", PersonalKind.VALUE, "优先温和表达")

    result_a = process.experience("a", "怎样指出朋友的错误？")
    result_b = process.experience("b", "怎样指出朋友的错误？")

    assert "优先坦率表达" in result_a.action_text
    assert "优先温和表达" in result_b.action_text
    assert result_a.action_text != result_b.action_text


def test_reflection_is_internal_cognition_not_external_fact(tmp_path):
    process, repository = runtime(tmp_path)
    process.experience("stone", "今天发生了一件重要的事")

    reflection = process.reflect("stone", "回顾我今天形成了什么理解")

    assert reflection.epistemic_status is EpistemicStatus.CONSIDERING
    assert repository.get_activity(reflection.activity_id).status is ActivityStatus.COMPLETED
    subject_history = repository.list_history("stone", HistoryKind.SUBJECT)
    assert subject_history[-1].event_type == "reflection"


def test_propose_open_matter_after_experience_links_thought(tmp_path):
    process, repository = runtime(tmp_path)
    result = process.experience("stone", "他看起来很累")
    open_matter = process.propose_open_matter(
        "stone",
        "继续关心他的疲倦",
        source_ids=(result.thought.id,),
    )
    assert open_matter.source_ids == (result.thought.id,)
    assert repository.list_history("stone", HistoryKind.SUBJECT)[-1].event_type == (
        "personal_item_created"
    )
