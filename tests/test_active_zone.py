"""02 活跃区与事件装载测试。

覆盖：首次输入空活跃区（初始化无特例、阶段② 不调用模型）、
预算内装载（未完成 + 完成）、超预算剔除（完成优先、旧→新）、
弹性一次、显式 id 召回恢复、模型聚焦回填活动挂载、preview 只读。
"""

from __future__ import annotations

from datetime import datetime, timezone

from jshi.activezone import InProcessActiveZone
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.recognition import ObjectProfile
from jshi.subject import (
    HistoryKind,
    PersonalItem,
    PersonalKind,
    PersonalStatus,
    SubjectProcess,
    SubjectRepository,
)


def runtime(tmp_path, model=None):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, model or CountingModel())
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="user", source="test", status="confirmed"
        )
    )
    return process, repository


class CountingModel:
    name = "counting-model"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(text="回应", model=self.name)


def add_event(
    repository: SubjectRepository,
    content: str,
    updated_at: datetime,
) -> PersonalItem:
    """直接写入一个事件本体（时间可指定，便于验证剔除顺序）。"""
    item = PersonalItem(
        subject_id="stone",
        kind=PersonalKind.CONCERN,
        content=content,
        source_ids=("seed",),
        created_at=updated_at,
        updated_at=updated_at,
    )
    repository.add_personal_item(item)
    return item


def evicted_records(repository: SubjectRepository):
    return [
        record
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
        if record.event_type == "event_evicted"
    ]


def test_first_input_empty_active_zone_same_path(tmp_path):
    model = CountingModel()
    process, repository = runtime(tmp_path, model=model)

    result = process.experience("stone", "你好", object_ref="user")

    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    loaded = [
        record for record in subject if record.event_type == "event_loaded"
    ]
    assert len(loaded) == 1
    assert loaded[0].content["event_ids"] == []
    assert loaded[0].content["budget"] == 8
    assert result.current_state.active_event_ids == ()
    assert result.current_state.active_zone.events == ()
    # 阶段② 不调用模型：全程仅认知阶段一次调用
    assert model.calls == 1


def test_active_zone_loads_unfinished_and_completed_within_budget(tmp_path):
    process, repository = runtime(tmp_path)
    first = add_event(repository, "事情甲", datetime(2026, 1, 1, tzinfo=timezone.utc))
    second = add_event(
        repository, "事情乙", datetime(2026, 1, 2, tzinfo=timezone.utc)
    )
    process.close_personal_item(second.id, PersonalStatus.COMPLETED, "了结")

    view = process.active_zone.load("stone", "你好")

    ids = [event.event_id for event in view.events]
    assert first.id in ids
    assert second.id in ids
    status_by_id = {event.event_id: event.status for event in view.events}
    assert status_by_id[first.id] == "unfinished"
    assert status_by_id[second.id] == "completed"


def test_evict_overflow_marks_oldest_and_keeps_body(tmp_path):
    process, repository = runtime(tmp_path)
    process.active_zone = InProcessActiveZone(repository, default_budget=2)
    e1 = add_event(repository, "事件1", datetime(2026, 1, 1, tzinfo=timezone.utc))
    add_event(repository, "事件2", datetime(2026, 1, 2, tzinfo=timezone.utc))
    add_event(repository, "事件3", datetime(2026, 1, 3, tzinfo=timezone.utc))
    add_event(repository, "事件4", datetime(2026, 1, 4, tzinfo=timezone.utc))

    process.experience("stone", "你好", object_ref="user")

    evicted = evicted_records(repository)
    assert len(evicted) == 1
    assert evicted[0].content["event_id"] == e1.id
    assert evicted[0].content["reason"] == "budget_overflow"
    item = repository.get_personal_item(e1.id)
    # 只打剔除标记：事件本体不删、状态不变
    assert item.metadata["zone_state"] == "evicted"
    assert item.status is PersonalStatus.ACTIVE
    assert item.content == "事件1"


def test_evict_overflow_prefers_completed_then_oldest(tmp_path):
    process, repository = runtime(tmp_path)
    process.active_zone = InProcessActiveZone(repository, default_budget=2)
    completed = add_event(
        repository, "旧完成", datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    process.close_personal_item(completed.id, PersonalStatus.COMPLETED, "了结")
    u1 = add_event(repository, "进行中1", datetime(2026, 1, 2, tzinfo=timezone.utc))
    add_event(repository, "进行中2", datetime(2026, 1, 3, tzinfo=timezone.utc))
    add_event(repository, "进行中3", datetime(2026, 1, 4, tzinfo=timezone.utc))
    add_event(repository, "进行中4", datetime(2026, 1, 5, tzinfo=timezone.utc))

    process.experience("stone", "你好", object_ref="user")

    ids = [record.content["event_id"] for record in evicted_records(repository)]
    # 总 5 条 > 预算 2 + 弹性 1 → 剔除 2 条：先完成事件，再最旧未完成
    assert ids == [completed.id, u1.id]


def test_elasticity_allows_one_overshoot_without_eviction(tmp_path):
    process, repository = runtime(tmp_path)
    process.active_zone = InProcessActiveZone(repository, default_budget=2)
    for index in range(1, 4):
        add_event(
            repository,
            f"事件{index}",
            datetime(2026, 1, index, tzinfo=timezone.utc),
        )

    process.experience("stone", "你好", object_ref="user")

    assert evicted_records(repository) == []


def test_recall_by_ids_restores_evicted_event_and_records(tmp_path):
    process, repository = runtime(tmp_path)
    process.active_zone = InProcessActiveZone(repository, default_budget=2)
    e1 = add_event(repository, "事件1", datetime(2026, 1, 1, tzinfo=timezone.utc))
    add_event(repository, "事件2", datetime(2026, 1, 2, tzinfo=timezone.utc))
    add_event(repository, "事件3", datetime(2026, 1, 3, tzinfo=timezone.utc))
    add_event(repository, "事件4", datetime(2026, 1, 4, tzinfo=timezone.utc))
    process.experience("stone", "你好", object_ref="user")
    assert repository.get_personal_item(e1.id).metadata["zone_state"] == "evicted"

    fragments = process.recall_events("stone", (e1.id,))

    assert len(fragments) == 1
    assert fragments[0].event_id == e1.id
    assert fragments[0].kind == "event"
    assert repository.get_personal_item(e1.id).metadata.get("zone_state") != "evicted"
    recalled = [
        record
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
        if record.event_type == "event_recalled"
    ]
    assert len(recalled) == 1
    assert recalled[0].content["event_ids"] == [e1.id]


def test_load_excludes_evicted_events(tmp_path):
    process, repository = runtime(tmp_path)
    process.active_zone = InProcessActiveZone(repository, default_budget=2)
    e1 = add_event(repository, "事件1", datetime(2026, 1, 1, tzinfo=timezone.utc))
    add_event(repository, "事件2", datetime(2026, 1, 2, tzinfo=timezone.utc))
    add_event(repository, "事件3", datetime(2026, 1, 3, tzinfo=timezone.utc))
    add_event(repository, "事件4", datetime(2026, 1, 4, tzinfo=timezone.utc))
    process.experience("stone", "你好", object_ref="user")

    view = process.active_zone.load("stone", "你好")

    assert e1.id not in [event.event_id for event in view.events]


class FocusedModel:
    name = "focused-model"

    def __init__(self, event_id: str) -> None:
        self.event_id = event_id

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="回应",
            model=self.name,
            focused_event_ids=(self.event_id,),
        )


def test_focused_event_ids_attach_to_activity(tmp_path):
    process, repository = runtime(tmp_path)
    concern = add_event(repository, "聚焦的事", datetime(2026, 1, 1, tzinfo=timezone.utc))
    process.cognition = FocusedModel(concern.id)

    result = process.experience("stone", "继续", object_ref="user")

    assert result.activity.active_concern_ids == (concern.id,)
    attached = [
        record
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
        if record.event_type == "activity_events_attached"
    ]
    assert len(attached) == 1
    assert attached[0].content["event_ids"] == [concern.id]
    assert attached[0].content["basis"] == "model_focus"


def test_preview_state_is_read_only_and_shows_active_zone(tmp_path):
    process, repository = runtime(tmp_path)
    concern = add_event(repository, "预览中的事件", datetime(2026, 1, 1, tzinfo=timezone.utc))

    preview = process.preview_state("stone", "你好", object_ref="user")

    assert [event.event_id for event in preview.active_zone.events] == [concern.id]
    assert preview.assembled.active_event_ids == (concern.id,)
    assert "预览中的事件" in preview.assembled.subject_state.concerns
    assert repository.list_history("stone") == ()
