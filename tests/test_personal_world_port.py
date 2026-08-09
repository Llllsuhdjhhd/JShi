"""个人世界装载系统（PersonalWorldPort）的测试。

覆盖：按重要程度排序、预算内截断、约束（承诺/开放事项）始终装载、
重要程度持久化、以及端口可替换（与记忆系统平行）。
"""

from __future__ import annotations

import pytest

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.personalworld import InProcessPersonalWorld
from jshi.subject import (
    PersonalItem,
    PersonalKind,
    SubjectProcess,
    SubjectRepository,
)


class FixedModel:
    name = "fixed-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="回应", model=self.name)


@pytest.fixture
def runtime(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    return repository, identities


def test_selection_ranks_context_items_by_importance(runtime):
    repository, identities = runtime
    world = InProcessPersonalWorld(repository)
    process = SubjectProcess(repository, identities, FixedModel(), personal_world=world)
    process.add_personal_item(
        "stone", PersonalKind.AESTHETIC, "低重要度审美", importance=0.2
    )
    process.add_personal_item(
        "stone", PersonalKind.VALUE, "高重要度价值", importance=0.9
    )
    process.add_personal_item(
        "stone", PersonalKind.CAPABILITY, "中重要度能力", importance=0.5
    )

    selected = world.select("stone", "任意输入")

    contents = [item.content for item in selected]
    assert contents.index("高重要度价值") < contents.index("中重要度能力")
    assert contents.index("中重要度能力") < contents.index("低重要度审美")


def test_budget_truncates_ranked_items_but_keeps_constraints(runtime):
    repository, identities = runtime
    world = InProcessPersonalWorld(repository)
    process = SubjectProcess(repository, identities, FixedModel(), personal_world=world)
    process.add_personal_item("stone", PersonalKind.VALUE, "价值甲", importance=0.9)
    process.add_personal_item("stone", PersonalKind.AESTHETIC, "审美乙", importance=0.8)
    process.add_personal_item(
        "stone", PersonalKind.SELF_UNDERSTANDING, "自我理解丙", importance=0.7
    )
    commitment = process.add_personal_item("stone", PersonalKind.COMMITMENT, "始终履约")
    concern = process.propose_open_matter(
        "stone", "继续追问", source_ids=("seed",)
    )

    selected = world.select("stone", "任意输入", budget=3)

    ids = {item.id for item in selected}
    assert commitment.id in ids
    assert concern.id in ids
    contents = [item.content for item in selected]
    assert "价值甲" in contents  # 预算内最高重要度条目
    assert "审美乙" not in contents
    assert "自我理解丙" not in contents


def test_importance_is_persisted_in_metadata(runtime):
    repository, identities = runtime
    process = SubjectProcess(repository, identities, FixedModel())

    item = process.add_personal_item(
        "stone", PersonalKind.VALUE, "重要价值", importance=0.8
    )
    stored = repository.get_personal_item(item.id)
    assert stored.metadata.get("importance") == 0.8

    default = process.add_personal_item("stone", PersonalKind.AESTHETIC, "默认条目")
    assert default.metadata == {}


def test_subject_process_uses_injected_port(runtime):
    repository, identities = runtime

    class FixedPort:
        def select(self, subject_id, query, *, budget=20):
            return (
                PersonalItem(
                    subject_id=subject_id,
                    kind=PersonalKind.VALUE,
                    content="仅此一条",
                ),
            )

    process = SubjectProcess(
        repository, identities, FixedModel(), personal_world=FixedPort()
    )

    assembled = process.assemble_current_state("stone", "输入")

    assert [item.content for item in assembled.personal_items] == ["仅此一条"]


def test_standing_constraints_returns_commitments_and_concerns(runtime):
    repository, identities = runtime
    process = SubjectProcess(repository, identities, FixedModel())
    process.add_personal_item("stone", PersonalKind.VALUE, "价值甲")
    commitment = process.add_personal_item(
        "stone", PersonalKind.COMMITMENT, "始终履约"
    )
    concern = process.propose_open_matter(
        "stone", "继续追问", source_ids=("seed",)
    )

    constraints = InProcessPersonalWorld(repository).standing_constraints("stone")

    ids = {item.id for item in constraints}
    assert commitment.id in ids
    assert concern.id in ids
    assert not any(item.kind is PersonalKind.VALUE for item in constraints)
