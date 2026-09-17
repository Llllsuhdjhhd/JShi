from __future__ import annotations

from jshi.action import InProcessActionRouter, PlaceholderRobotAction
from jshi.activityclose import InProcessActivityClose
from jshi.evaluation import EvaluationEvent, InProcessEvaluationSystem
from jshi.models import ResponseItem, ResponsePlan
from jshi.objects import InProcessObjectSystem
from jshi.recognition import CarrierEntry, ObjectProfileRepository
from jshi.subject import Activity, ActivityKind, ActivityStatus, HistoryKind, SubjectRepository


def test_activity_close_completes_and_audits(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    activity = Activity(
        subject_id="stone",
        kind=ActivityKind.EXTERNAL,
        trigger="fact-1",
        response_statuses=("verbal",),
    )
    repository.add_activity(activity)

    result = InProcessActivityClose(repository).close(
        "stone",
        activity.id,
        final_response_statuses=("verbal",),
        action_id="action-1",
        reason="done",
    )

    stored = repository.get_activity(result.activity_id)
    assert stored.status == ActivityStatus.COMPLETED
    assert stored.response_statuses == ("verbal",)
    assert result.handoff_to_memory_control is True
    assert result.transition_id
    assert any(
        record.event_type == "activity_completed"
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
    )


def test_evaluation_system_collects_events():
    system = InProcessEvaluationSystem()
    event = EvaluationEvent(
        event_id="e1",
        subject_id="stone",
        activity_id="a1",
        event_type="recall_executed",
    )
    system.emit(event)
    assert system.collect("stone") == (event,)


def test_object_system_uses_existing_repository(tmp_path):
    repository = ObjectProfileRepository(tmp_path / "profiles.sqlite3")
    system = InProcessObjectSystem(repository)
    profile = system.ensure_provisional(
        object_id="OBJ-A",
        label="user",
        source="test",
        carriers=(CarrierEntry(kind="session", value="s1"),),
    )
    assert profile.status == "provisional"
    assert system.confirm("OBJ-A").status == "confirmed"
    assert system.deny("OBJ-A").status == "rejected"


def test_action_router_triggers_robot_for_embodied(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    router = InProcessActionRouter(repository, PlaceholderRobotAction())

    result = router.dispatch(
        subject_id="stone",
        activity_id="a1",
        action_text="点头",
        model="m",
        source_id="activity-1",
        response_plan=ResponsePlan(
            mode="respond",
            items=(ResponseItem(channel="embodied", text="点头"),),
        ),
    )

    assert result.robot_action_triggered is True
    assert result.speech_triggered is False
    assert result.action_id == ""
    facts = repository.list_history("stone", HistoryKind.FACT)
    assert all(record.event_type != "language_action" for record in facts)


class RecordingSpeech:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def speak(self, *, subject_id, activity_id, text):
        del subject_id, activity_id
        self.texts.append(text)


class RecordingRobot:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def trigger_embodied_action(self, *, subject_id, activity_id, action_text, response_statuses):
        del subject_id, activity_id, response_statuses
        self.calls.append(action_text)


def test_verbal_records_language_and_calls_speech_not_robot(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    speech = RecordingSpeech()
    robot = RecordingRobot()
    router = InProcessActionRouter(repository, robot, speech)

    result = router.dispatch(
        subject_id="stone",
        activity_id="a1",
        action_text="你好",
        model="m",
        source_id="activity-1",
        response_plan=ResponsePlan(
            mode="respond",
            items=(ResponseItem(channel="verbal", text="你好"),),
        ),
    )

    assert result.speech_triggered is True
    assert result.robot_action_triggered is False
    assert speech.texts == ["你好"]
    assert robot.calls == []
    facts = repository.list_history("stone", HistoryKind.FACT)
    assert facts[0].event_type == "language_action"
    assert facts[0].content["text"] == "你好"


def test_wait_does_not_speak(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    speech = RecordingSpeech()
    router = InProcessActionRouter(repository, PlaceholderRobotAction(), speech)

    result = router.dispatch(
        subject_id="stone",
        activity_id="a1",
        action_text="",
        model="m",
        source_id="activity-1",
        response_plan=ResponsePlan(mode="wait", reason="等下一条"),
    )

    assert result.speech_triggered is False
    assert speech.texts == []
    assert result.action_id == ""
