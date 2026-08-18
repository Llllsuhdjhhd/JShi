"""07 关闭活动：一律 completed；不写经历、不改活跃区、不投递记忆。"""

from __future__ import annotations

from jshi.activityclose import InProcessActivityClose
from jshi.experienceledger import InProcessExperienceLedger
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse, ResponsePlan
from jshi.recognition import ObjectProfile
from jshi.subject import (
    Activity,
    ActivityKind,
    ActivityStatus,
    HistoryKind,
    SubjectProcess,
    SubjectRepository,
)


class PlanModel:
    name = "plan-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(
            model=self.name,
            response_plan=ResponsePlan(mode="wait", reason="等下一条"),
        )


def test_close_completes_and_hands_off(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    activity = Activity(
        subject_id="stone",
        kind=ActivityKind.EXTERNAL,
        trigger="fact-1",
        response_statuses=("wait",),
    )
    repository.add_activity(activity)

    result = InProcessActivityClose(repository).close(
        "stone",
        activity.id,
        final_response_statuses=("wait",),
        action_id="action-1",
        reason="external_activity_finished_mode_wait",
    )

    stored = repository.get_activity(result.activity_id)
    assert stored.status == ActivityStatus.COMPLETED
    assert stored.response_statuses == ("wait",)
    assert result.final_activity_status == "completed"
    assert result.handoff_to_memory_control is True
    assert result.transition_id
    transitions = repository.list_transitions(activity.id)
    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("open", "completed")
    ]
    assert result.transition_id == transitions[-1].id


def test_second_close_is_idempotent(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    activity = Activity(
        subject_id="stone",
        kind=ActivityKind.EXTERNAL,
        trigger="fact-1",
    )
    repository.add_activity(activity)
    closer = InProcessActivityClose(repository)
    first = closer.close(
        "stone",
        activity.id,
        final_response_statuses=("respond", "verbal"),
        action_id="",
        reason="done",
    )
    second = closer.close(
        "stone",
        activity.id,
        final_response_statuses=("wait",),
        action_id="",
        reason="again",
    )

    stored = repository.get_activity(activity.id)
    assert stored.status == ActivityStatus.COMPLETED
    assert stored.response_statuses == ("respond", "verbal")
    assert first.handoff_to_memory_control is True
    assert second.handoff_to_memory_control is False
    completed = [
        item
        for item in repository.list_history("stone", HistoryKind.SUBJECT)
        if item.event_type == "activity_completed"
    ]
    assert len(completed) == 1


def test_close_does_not_write_experience_or_change_view(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    ledger = InProcessExperienceLedger()
    activity = Activity(
        subject_id="stone",
        kind=ActivityKind.EXTERNAL,
        trigger="fact-1",
    )
    repository.add_activity(activity)
    ledger.append_external(
        "stone",
        actor_object_id="OBJ-USER",
        text_raw="先说到这里",
    )
    view_before = ledger.current_context_view("stone")
    experiences_before = ledger.list_experiences("stone")

    InProcessActivityClose(repository).close(
        "stone",
        activity.id,
        final_response_statuses=("wait",),
        action_id="",
        reason="done",
    )

    assert ledger.current_context_view("stone") == view_before
    assert ledger.list_experiences("stone") == experiences_before
    assert repository.list_history("stone", HistoryKind.FACT) == ()


def test_wait_and_internal_close_are_completed(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, PlanModel())
    process.profiles.create(
        ObjectProfile(object_id="OBJ-USER", label="user", source="test", status="confirmed")
    )

    waited = process.experience("stone", "先到这里", object_ref="user")
    reflection = process.reflect("stone", "回顾一下")

    assert waited.activity.status == ActivityStatus.COMPLETED
    assert "wait" in waited.activity.response_statuses
    stored = repository.get_activity(reflection.activity_id)
    assert stored.status == ActivityStatus.COMPLETED
    completed = [
        item
        for item in repository.list_history("stone", HistoryKind.SUBJECT)
        if item.event_type == "activity_completed"
    ]
    assert len(completed) == 2
    assert {item.content["reason"] for item in completed} == {
        "external_activity_finished_mode_wait",
        "internal_activity_finished",
    }
