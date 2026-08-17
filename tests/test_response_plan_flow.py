"""response_plan 约束行动：think/ignore/wait 不说话，活动仍结束。"""

from __future__ import annotations

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse, ResponsePlan
from jshi.recognition import ObjectProfile
from jshi.subject import ActivityStatus, HistoryKind, SubjectProcess, SubjectRepository


class PlanModel:
    name = "plan-model"

    def __init__(self, plan: ResponsePlan) -> None:
        self._plan = plan

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(model=self.name, response_plan=self._plan)


def runtime(tmp_path, plan: ResponsePlan):
    tmp_path.mkdir(parents=True, exist_ok=True)
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, PlanModel(plan))
    process.profiles.create(
        ObjectProfile(object_id="OBJ-USER", label="user", source="test", status="confirmed")
    )
    return process, repository


def test_wait_completes_activity_without_language_action(tmp_path):
    process, repository = runtime(
        tmp_path, ResponsePlan(mode="wait", reason="等对方下一条")
    )

    first = process.experience("stone", "先说到这里", object_ref="user")
    second = process.experience("stone", "我回来了", object_ref="user")

    facts = repository.list_history("stone", HistoryKind.FACT)
    assert "language_action" not in [item.event_type for item in facts]
    assert first.activity.status == ActivityStatus.COMPLETED
    assert second.activity.status == ActivityStatus.COMPLETED
    assert first.activity.id != second.activity.id
    assert first.action_text == ""
    assert "wait" in first.activity.response_statuses


def test_think_and_ignore_do_not_speak_but_still_close(tmp_path):
    for mode in ("think", "ignore"):
        process, repository = runtime(
            tmp_path / mode, ResponsePlan(mode=mode, reason=mode)
        )
        result = process.experience("stone", "你好", object_ref="user")
        facts = repository.list_history("stone", HistoryKind.FACT)
        assert "language_action" not in [item.event_type for item in facts]
        assert result.activity.status == ActivityStatus.COMPLETED
        assert result.action_text == ""
        assert mode in result.activity.response_statuses
