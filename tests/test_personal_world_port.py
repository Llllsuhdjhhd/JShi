"""08 薄壳：合并有序列表、按 id 去重、按等级取量。"""

from __future__ import annotations

import pytest

from jshi.assembly import (
    AssemblyContext,
    AssemblySpeaker,
    CurrentStateAssembler,
    IdentitySource,
    PersonalWorldSource,
)
from jshi.experienceledger import ContextViewState
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


def test_select_keeps_module_order(runtime):
    repository, identities = runtime
    world = InProcessPersonalWorld(repository)
    process = SubjectProcess(repository, identities, FixedModel(), personal_world=world)
    process.add_personal_item("stone", PersonalKind.VALUE, "价值甲", importance=0.9)
    process.add_personal_item("stone", PersonalKind.VALUE, "价值乙", importance=0.2)
    process.add_personal_item("stone", PersonalKind.AESTHETIC, "审美")
    commitment = process.add_personal_item("stone", PersonalKind.COMMITMENT, "始终履约")

    selected = world.select("stone", "朋友")
    ids = [item.id for item in selected]
    kinds = [item.kind for item in selected]

    assert commitment.id in ids
    assert kinds.index(PersonalKind.COMMITMENT) < kinds.index(PersonalKind.VALUE)
    contents = [item.content for item in selected]
    assert contents.index("价值甲") < contents.index("价值乙")
    assert world.select("stone", "无关查询") == selected


def test_load_level_low_takes_fewer_ordinary_items(runtime):
    repository, identities = runtime
    world = InProcessPersonalWorld(repository)
    process = SubjectProcess(repository, identities, FixedModel(), personal_world=world)
    for index in range(6):
        process.add_personal_item(
            "stone",
            PersonalKind.VALUE,
            f"价值{index}",
            importance=1.0 - index * 0.05,
            level="中",
        )
    commitment = process.add_personal_item("stone", PersonalKind.COMMITMENT, "始终履约")

    low = world.select("stone", load_level="低")
    mid = world.select("stone", load_level="中")

    assert commitment.id in {item.id for item in low}
    ordinary_low = [item for item in low if item.kind is PersonalKind.VALUE]
    ordinary_mid = [item for item in mid if item.kind is PersonalKind.VALUE]
    assert len(ordinary_low) == 1
    assert len(ordinary_mid) >= 4
    assert len(ordinary_low) < len(ordinary_mid)


def test_standing_keeps_commitment_and_boundary(runtime):
    repository, identities = runtime
    process = SubjectProcess(repository, identities, FixedModel())
    process.add_personal_item("stone", PersonalKind.VALUE, "价值甲")
    commitment = process.add_personal_item("stone", PersonalKind.COMMITMENT, "始终履约")
    boundary = PersonalItem(
        subject_id="stone",
        kind=PersonalKind.VALUE,
        content="不可编造事实",
        metadata={"role": "boundary", "binding": True},
    )
    repository.add_personal_item(boundary)

    world = InProcessPersonalWorld(repository)
    constraints = world.standing_constraints("stone")
    ids = {item.id for item in constraints}
    assert commitment.id in ids
    assert boundary.id in ids
    selected = world.select("stone")
    assert list(ids).count(boundary.id) == 1
    assert [item.id for item in selected].count(boundary.id) == 1
    assert [item.id for item in selected].count(commitment.id) == 1


def test_does_not_duplicate_same_id_from_modules(runtime):
    repository, _identities = runtime
    item = PersonalItem(
        subject_id="stone",
        kind=PersonalKind.VALUE,
        content="同一条",
        metadata={"role": "boundary", "binding": True},
    )
    repository.add_personal_item(item)
    world = InProcessPersonalWorld(repository)
    selected = world.select("stone")
    assert [entry.id for entry in selected].count(item.id) == 1


def test_subject_process_uses_injected_port(runtime):
    repository, identities = runtime

    class FixedPort:
        def select(self, subject_id, query="", *, load_level="中"):
            del query, load_level
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


def test_assembly_skips_personal_ids_already_in_active_zone(runtime):
    repository, identities = runtime
    world = InProcessPersonalWorld(repository)
    process = SubjectProcess(repository, identities, FixedModel(), personal_world=world)
    value = process.add_personal_item("stone", PersonalKind.VALUE, "优先坦率表达")
    assembler = CurrentStateAssembler(
        sources=(
            IdentitySource(identities),
            PersonalWorldSource(world),
        )
    )
    ws = assembler.assemble(
        AssemblyContext(
            subject_id="stone",
            input_text="你好",
            speaker=AssemblySpeaker(object_id="OBJ-USER", label="user"),
            context_view=ContextViewState(
                segment_refs=(f"personal:{value.id}",),
            ),
        )
    )
    personal_ids = {
        fragment.id for fragment in ws.fragments if fragment.source == "personal"
    }
    assert f"personal:{value.id}" not in personal_ids
    report = {item.source: item for item in ws.report}
    assert f"personal:{value.id}" in report["personal"].skipped_ids
