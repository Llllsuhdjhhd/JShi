"""06 只校验并标记 response_plan，不写经历、不改活跃区、不关闭活动。"""

from __future__ import annotations

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse, ResponseItem, ResponsePlan
from jshi.responsemark import evaluate_response_plan
from jshi.subject import (
    Activity,
    ActivityKind,
    ActivityStatus,
    HistoryKind,
    SubjectProcess,
    SubjectRepository,
)


class UnusedModel:
    name = "unused-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        raise AssertionError("06 标记不得调用模型")


def test_wait_plus_verbal_is_ignored():
    result = evaluate_response_plan(
        ResponsePlan(
            mode="wait",
            reason="等确认",
            items=(ResponseItem(channel="verbal", text="不该说出来"),),
        )
    )
    assert result.final == ("wait",)
    assert result.illegal_channels == ("verbal",)
    assert result.missing_items is False


def test_respond_without_items_records_missing_items():
    result = evaluate_response_plan(ResponsePlan(mode="respond"))
    assert result.final == ("respond",)
    assert result.missing_items is True
    assert result.missing_reason is False


def test_silent_mode_without_reason_is_recorded():
    result = evaluate_response_plan(ResponsePlan(mode="think"))
    assert result.final == ("think",)
    assert result.missing_reason is True


def test_human_override_drops_unknown_and_illegal_verbal():
    result = evaluate_response_plan(
        ResponsePlan(mode="respond", items=(ResponseItem(channel="verbal", text="嗨"),)),
        human_override=("wait", "verbal", "unknown-mode"),
    )
    assert result.final == ("wait",)
    assert result.source == "human_overridden"
    assert "verbal" in result.illegal_channels
    assert "unknown-mode" in result.unknown


def test_mark_does_not_write_experience_or_change_view_or_lifecycle(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, UnusedModel())
    activity = Activity(
        subject_id="stone",
        kind=ActivityKind.EXTERNAL,
        trigger="fact-1",
    )
    repository.add_activity(activity)
    process.activity_ledger.append_external(
        "stone",
        actor_object_id="OBJ-USER",
        text_raw="先说到这里",
    )
    view_before = process.activity_ledger.current_context_view("stone")
    experiences_before = process.activity_ledger.list_experiences("stone")

    updated, final, unknown = process.mark_activity_response_status(
        "stone",
        activity,
        ResponsePlan(mode="wait", reason="等下一条"),
    )

    assert updated.status == ActivityStatus.OPEN
    assert final == ("wait",)
    assert unknown == ()
    assert process.activity_ledger.current_context_view("stone") == view_before
    assert process.activity_ledger.list_experiences("stone") == experiences_before
    assert repository.list_history("stone", HistoryKind.FACT) == ()
    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    assert [item.event_type for item in subject] == ["activity_response_state"]
    assert subject[0].content["missing_items"] is False
