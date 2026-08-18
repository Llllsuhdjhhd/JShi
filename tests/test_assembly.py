"""03 状态组装测试。

覆盖：统一装载片段与源报告、事件/个人世界去重、常驻内容、
预算截断与跳过报告、记忆源对象过滤与低档、占位源标记、
单源失败隔离、记账与 preview 只读。
"""

from __future__ import annotations

from jshi.assembly import (
    ActivityWindowSource,
    AssemblyContext,
    CurrentStateAssembler,
    EpistemicSource,
    IdentitySource,
    MemorySource,
    ObjectSource,
    PersonalWorldSource,
)
from jshi.experienceledger import empty_context_view
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.personalworld import InProcessPersonalWorld
from jshi.recognition import ObjectProfile
from jshi.subject import (
    HistoryKind,
    HistoryRecord,
    PersonalItem,
    PersonalKind,
    SubjectProcess,
    SubjectRepository,
)


class FixedModel:
    name = "fixed-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="回应", model=self.name)


def runtime(tmp_path, model=None):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, model or FixedModel())
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="user", source="test", status="confirmed"
        )
    )
    return process, repository, identities


def test_process_assembly_builds_fragments_and_report(tmp_path):
    process, _repository, _identities = runtime(tmp_path)
    process.propose_open_matter(
        "stone", "继续理解朋友的疲倦", source_ids=("seed",)
    )
    process.add_personal_item("stone", PersonalKind.VALUE, "优先坦率表达")
    process.add_personal_item("stone", PersonalKind.COMMITMENT, "下次继续询问")

    result = process.experience("stone", "你好", object_ref="user")

    fragments = result.current_state.fragments
    assert any(
        fragment.source == "identity"
        and fragment.kind == "identity_summary"
        and fragment.always
        for fragment in fragments
    )
    second = process.experience("stone", "继续", object_ref="user")
    assert any(
        fragment.source == "activity" for fragment in second.current_state.fragments
    )
    assert any(
        fragment.source == "personal" and fragment.kind == "commitment"
        for fragment in fragments
    )
    report = {item.source: item for item in result.current_state.source_report}
    assert set(report) == {
        "identity",
        "activity",
        "personal",
        "memory",
        "epistemic",
        "object",
    }
    assert report["epistemic"].status == "placeholder"
    assert report["object"].status == "placeholder"
    assert report["memory"].status == "implemented"


def test_activity_window_not_in_personal(tmp_path):
    process, _repository, _identities = runtime(tmp_path)

    process.experience("stone", "你好", object_ref="user")
    result = process.experience("stone", "继续", object_ref="user")

    activity_ids = [
        fragment.id
        for fragment in result.current_state.fragments
        if fragment.source == "activity"
    ]
    personal_kinds = {
        fragment.kind
        for fragment in result.current_state.fragments
        if fragment.source == "personal"
    }
    assert activity_ids
    assert "concern" not in personal_kinds


def test_budget_truncation_keeps_always_and_reports_skipped(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    personal_world = InProcessPersonalWorld(repository)
    assembler = CurrentStateAssembler(
        sources=(
            IdentitySource(identities),
            ActivityWindowSource(),
            PersonalWorldSource(personal_world),
            EpistemicSource(),
            ObjectSource(),
        )
    )
    for index in range(5):
        repository.add_personal_item(
            repository_personal_item(
                repository, f"价值{index}", PersonalKind.VALUE
            )
        )
    commitment = repository_personal_item(
        repository, "常驻承诺", PersonalKind.COMMITMENT
    )
    repository.add_personal_item(commitment)

    ctx = AssemblyContext(
        subject_id="stone",
        input_text="你好",
        object_id="OBJ-USER",
        context_view=empty_context_view(),
        budget_extra=1,
    )
    ws = assembler.assemble(ctx)

    values = [
        fragment for fragment in ws.fragments
        if fragment.source == "personal" and fragment.kind == "value"
    ]
    assert len(values) == 1  # 额外预算 1：只保留一个非常驻条目
    assert commitment.id in {fragment.id for fragment in ws.fragments}
    report = {item.source: item for item in ws.report}
    assert len(report["personal"].skipped_ids) == 4
    assert report["personal"].budget == 1


def repository_personal_item(repository, content, kind):
    from jshi.subject import PersonalItem

    item = PersonalItem(
        subject_id="stone",
        kind=kind,
        content=content,
        source_ids=("seed",),
    )
    return item


def test_memory_source_filters_by_object_and_recency(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    for index in range(5):
        repository.add_history(
            HistoryRecord(
                subject_id="stone",
                kind=HistoryKind.FACT,
                event_type="external_input",
                content={
                    "text": f"对象A的第{index}条",
                    "object_id": "OBJ-A",
                },
            )
        )
    repository.add_history(
        HistoryRecord(
            subject_id="stone",
            kind=HistoryKind.FACT,
            event_type="external_input",
            content={
                "text": "对象B的往事",
                "object_id": "OBJ-B",
            },
        )
    )
    source = MemorySource(repository)

    ctx = AssemblyContext(
        subject_id="stone",
        input_text="随便聊聊",
        object_id="OBJ-A",
        context_view=empty_context_view(),
        budget_extra=4,
        recall_level=1,
    )
    low = source.load(ctx).fragments
    assert len(low) == 3  # 低档 1–3 → 3 条线索
    assert all(fragment.id for fragment in low)
    assert "对象A的第4条" in {fragment.content for fragment in low}
    assert "对象B的往事" not in {fragment.content for fragment in low}

    deep = source.load(
        AssemblyContext(
            subject_id="stone",
            input_text="随便聊聊",
            object_id="OBJ-A",
            context_view=empty_context_view(),
            budget_extra=4,
            recall_level=8,
        )
    ).fragments
    assert len(deep) == 5  # 高档 7–9 → 8 条上限，实际 5 条


def test_memory_source_skips_without_object_id(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    source = MemorySource(repository)

    ctx = AssemblyContext(
        subject_id="stone",
        input_text="你好",
        object_id=None,
        context_view=empty_context_view(),
        budget_extra=4,
    )
    assert source.load(ctx).fragments == ()


class BadSource:
    name = "bad"
    status = "implemented"

    def load(self, ctx: AssemblyContext):
        raise RuntimeError("boom")


def test_single_source_failure_is_isolated(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    assembler = CurrentStateAssembler(
        sources=(
            BadSource(),
            IdentitySource(identities),
        )
    )

    ws = assembler.assemble(
        AssemblyContext(
            subject_id="stone",
            input_text="你好",
            context_view=empty_context_view(),
            budget_extra=4,
        )
    )

    assert ws.fragments  # 其余源正常
    report = {item.source: item for item in ws.report}
    assert report["bad"].error == "boom"
    assert report["bad"].loaded_ids == ()
    assert report["identity"].error is None


def test_current_state_assembled_records_sources(tmp_path):
    process, repository, _identities = runtime(tmp_path)

    process.experience("stone", "你好", object_ref="user")

    records = [
        record
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
        if record.event_type == "current_state_assembled"
    ]
    assert len(records) == 1
    sources = records[0].content["sources"]
    source_names = {item["source"] for item in sources}
    assert source_names == {
        "identity",
        "activity",
        "personal",
        "memory",
        "epistemic",
        "object",
    }
    assert all(item["status"] in {"implemented", "placeholder"} for item in sources)


def test_preview_state_reports_sources_and_stays_read_only(tmp_path):
    process, repository, _identities = runtime(tmp_path)

    preview = process.preview_state("stone", "你好", object_ref="user")

    assert len(preview.assembled.source_report) == 6
    assert repository.list_history("stone") == ()


def test_personal_world_source_marks_binding_boundary_always(tmp_path):
    process, repository, _identities = runtime(tmp_path)
    boundary = PersonalItem(
        subject_id="stone",
        kind=PersonalKind.VALUE,
        content="不可编造事实",
        metadata={"role": "boundary", "binding": True},
    )
    repository.add_personal_item(boundary)
    process.add_personal_item("stone", PersonalKind.VALUE, "优先坦率表达", importance=0.9)

    result = process.experience("stone", "你好", object_ref="user")

    boundary_fragments = [
        fragment
        for fragment in result.current_state.fragments
        if fragment.id == boundary.id
    ]
    assert boundary_fragments
    assert boundary_fragments[0].kind == "boundary"
    assert boundary_fragments[0].always is True
    assert "优先坦率表达" in {
        fragment.content
        for fragment in result.current_state.fragments
        if fragment.source == "personal"
    }
