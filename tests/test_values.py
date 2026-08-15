"""100 价值观与边界的完整测试。"""

from __future__ import annotations

from jshi.personalworld import InProcessValues, ValueSource
from jshi.subject import PersonalItem, PersonalKind, PersonalStatus, SubjectRepository


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


def propose_value(world, content, **kwargs):
    return world.propose_value(
        "stone",
        content,
        source_type=ValueSource.CLASSIC_WORK,
        **kwargs,
    )


def test_propose_value_creates_candidate_not_loadable(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    world = InProcessValues(repository)

    item = propose_value(
        world,
        "与朋友交，言而有信。",
        summary="关于诚信",
        domains=("friend",),
    )

    assert item.status is PersonalStatus.CANDIDATE
    assert item.metadata["summary"] == "关于诚信"
    assert world.select_values("stone", "朋友") == ()


def test_review_accept_makes_value_loadable(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    world = InProcessValues(repository)
    item = propose_value(world, "与朋友交，言而有信。", summary="关于诚信")

    accepted = world.review_value(item.id, "accept", "讨论定稿", "stone")

    assert accepted.status is PersonalStatus.ACCEPTED
    assert item.id in {value.id for value in world.select_values("stone", "朋友")}


def test_review_reject_keeps_value_out(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    world = InProcessValues(repository)
    item = propose_value(world, "不应采纳的原则。")

    rejected = world.review_value(item.id, "reject", "来源不足", "stone")

    assert rejected.status is PersonalStatus.REJECTED
    assert item.id not in {value.id for value in world.select_values("stone", "原则")}


def test_lock_and_supersede_lifecycle(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    world = InProcessValues(repository)
    old = propose_value(world, "旧原则", summary="旧原则")
    world.review_value(old.id, "accept", "接受", "stone")
    locked = world.lock_value(old.id, "稳定下来", "stone")

    replacement = propose_value(world, "新原则", summary="新原则")
    world.review_value(replacement.id, "accept", "接受", "stone")
    superseded = world.supersede_value(old.id, replacement.id, "被新原则替代")

    assert locked.status is PersonalStatus.LOCKED
    assert superseded.status is PersonalStatus.SUPERSEDED
    selected_ids = {value.id for value in world.select_values("stone", "原则")}
    assert old.id not in selected_ids
    assert replacement.id in selected_ids


def test_import_export_roundtrip(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    world = InProcessValues(repository)
    entries = [
        {
            "role": "value",
            "summary": "关于诚信",
            "content": "与朋友交，言而有信。",
            "stance": "在朋友关系中更看重兑现承诺。",
            "source_type": "classic_work",
            "source_ids": ["论语·学而"],
            "domains": ["friend"],
            "context_tags": ["承诺", "朋友"],
            "importance": 0.9,
            "status": "accepted",
        },
        {
            "role": "boundary",
            "summary": "不破坏不可逆自然",
            "content": "不因一时便利破坏不可逆的自然环境。",
            "stance": "不可逆损失无法用事后收益弥补。",
            "source_type": "classic_work",
            "domains": ["nature"],
            "binding": True,
            "status": "locked",
        },
    ]

    report = world.import_entries("stone", entries)
    exported = world.export_document("stone")

    assert len(report.imported_ids) == 2
    assert exported["schema_version"] == 1
    assert {entry["summary"] for entry in exported["entries"]} == {
        "关于诚信",
        "不破坏不可逆自然",
    }


def test_consolidate_detects_duplicate_and_is_cursor_resumable(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    world = InProcessValues(repository)
    world.import_entries(
        "stone",
        [
            {
                "role": "value",
                "content": "与朋友交，言而有信。",
                "source_type": "classic_work",
                "domains": ["friend"],
                "status": "accepted",
            },
            {
                "role": "value",
                "content": "与朋友交，言而有信。",
                "source_type": "classic_novel",
                "domains": ["friend"],
                "status": "accepted",
            },
            {
                "role": "value",
                "content": "过于抽象的说法。",
                "source_type": "model_proposal",
                "status": "candidate",
            },
        ],
    )

    first = world.consolidate("stone", budget=20, cursor=0)
    second = world.consolidate("stone", budget=20, cursor=first.cursor)

    assert any(suggestion.type == "merge" for suggestion in first.suggestions)
    assert any(suggestion.type == "abstract" for suggestion in first.suggestions)
    assert first.processed_count == 3
    assert second.processed_count == 0
    assert second.suggestions == ()
