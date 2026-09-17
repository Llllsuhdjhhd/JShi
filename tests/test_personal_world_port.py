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
from tests.value_seed import accepted_boundary, accepted_value, import_values


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
    import_values(world, "stone", [
        accepted_value("价值甲", importance=0.9),
        accepted_value("价值乙", importance=0.2),
    ])
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


def test_load_level_low_takes_fewer_remainder_items(runtime):
    repository, identities = runtime
    world = InProcessPersonalWorld(repository)
    process = SubjectProcess(repository, identities, FixedModel(), personal_world=world)
    import_values(world, "stone", [accepted_value("一条价值")])
    for index in range(6):
        process.add_personal_item(
            "stone",
            PersonalKind.AESTHETIC,
            f"审美{index}",
            importance=1.0 - index * 0.05,
            level="中",
        )
    commitment = process.add_personal_item("stone", PersonalKind.COMMITMENT, "始终履约")

    low = world.select("stone", load_level="低")
    mid = world.select("stone", load_level="中")

    assert commitment.id in {item.id for item in low}
    ordinary_low = [item for item in low if item.kind is PersonalKind.AESTHETIC]
    ordinary_mid = [item for item in mid if item.kind is PersonalKind.AESTHETIC]
    assert len(ordinary_low) == 1
    assert len(ordinary_mid) >= 4
    assert len(ordinary_low) < len(ordinary_mid)


def test_standing_keeps_commitment_and_boundary(runtime):
    repository, identities = runtime
    process = SubjectProcess(repository, identities, FixedModel())
    import_values(process, "stone", [accepted_value("价值甲")])
    commitment = process.add_personal_item("stone", PersonalKind.COMMITMENT, "始终履约")
    report = import_values(
        process, "stone", [accepted_boundary("不可编造事实")]
    )
    boundary_id = report.imported_ids[0]

    world = InProcessPersonalWorld(repository)
    constraints = world.standing_constraints("stone")
    ids = {item.id for item in constraints}
    assert commitment.id in ids
    assert boundary_id in ids
    selected = world.select("stone")
    assert list(ids).count(boundary_id) == 1
    assert [item.id for item in selected].count(boundary_id) == 1
    assert [item.id for item in selected].count(commitment.id) == 1


def test_does_not_duplicate_same_id_from_modules(runtime):
    repository, _identities = runtime
    world = InProcessPersonalWorld(repository)
    report = import_values(world, "stone", [accepted_boundary("同一条")])
    item_id = report.imported_ids[0]
    selected = world.select("stone")
    assert [entry.id for entry in selected].count(item_id) == 1


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


def test_assembly_still_loads_personal_when_zone_has_old_id(runtime):
    repository, identities = runtime
    world = InProcessPersonalWorld(repository)
    process = SubjectProcess(repository, identities, FixedModel(), personal_world=world)
    report = import_values(world, "stone", [accepted_value("优先坦率表达")])
    value_id = report.imported_ids[0]
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
                segment_refs=(f"personal:{value_id}",),
            ),
        )
    )
    personal_ids = {
        fragment.id for fragment in ws.fragments if fragment.source == "personal"
    }
    assert f"personal:{value_id}" in personal_ids
