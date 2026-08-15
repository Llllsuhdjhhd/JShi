"""100 价值观与边界的装载侧测试。"""

from __future__ import annotations

from jshi.personalworld import InProcessValues
from jshi.subject import PersonalItem, PersonalKind, SubjectRepository


def add_item(
    repository,
    content,
    *,
    kind=PersonalKind.VALUE,
    importance=1.0,
    metadata=None,
):
    meta = dict(metadata or {})
    if importance != 1.0:
        meta["importance"] = importance
    item = PersonalItem(
        subject_id="stone",
        kind=kind,
        content=content,
        metadata=meta,
    )
    repository.add_personal_item(item)
    return item


def test_select_values_ranks_importance_then_relevance(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    world = InProcessValues(repository)
    add_item(repository, "朋友相关但低重要", importance=0.2)
    add_item(repository, "高重要普通内容", importance=0.9)
    add_item(repository, "普通审美", importance=0.5)

    selected = world.select_values("stone", "朋友")

    contents = [item.content for item in selected]
    assert contents.index("高重要普通内容") < contents.index("普通审美")
    assert contents.index("普通审美") < contents.index("朋友相关但低重要")


def test_select_values_excludes_boundaries(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    world = InProcessValues(repository)
    add_item(repository, "优先坦率表达", importance=0.9)
    add_item(
        repository,
        "不可编造事实",
        metadata={"role": "boundary", "binding": True},
    )

    selected = world.select_values("stone", "任意输入")

    assert [item.content for item in selected] == ["优先坦率表达"]


def test_select_boundaries_and_standing_constraints(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    world = InProcessValues(repository)
    boundary = add_item(
        repository,
        "不可编造事实",
        metadata={"role": "boundary", "binding": True},
    )
    add_item(repository, "优先坦率表达")

    assert world.select_boundaries("stone") == (boundary,)
    assert world.standing_constraints("stone") == (boundary,)


def test_non_binding_boundary_is_not_standing_constraint(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    world = InProcessValues(repository)
    add_item(
        repository,
        "软性边界",
        metadata={"role": "boundary", "binding": False},
    )

    assert world.select_boundaries("stone") == ()
    assert world.standing_constraints("stone") == ()
