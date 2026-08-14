"""05 认知与追加召回测试。

覆盖：追加最多一轮、指标记账（recall_metrics）、回忆评价消费
（recall_evaluated）、引用率（recall_reference）、档位记录、无追加无指标。
"""

from __future__ import annotations

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import (
    ModelRequest,
    ModelResponse,
    RecallEvaluation,
    RecallRequest,
)
from jshi.recognition import ObjectProfile
from jshi.subject import (
    HistoryKind,
    SubjectProcess,
    SubjectRepository,
)


class FixedModel:
    name = "fixed-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="回应", model=self.name)


class FollowupModel:
    name = "followup-model"

    def __init__(self, *, level: int = 1, evaluate: bool = False) -> None:
        self.calls = 0
        self.level = level
        self.evaluate = evaluate

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="需要更多过去",
                model=self.name,
                recall_requests=(
                    RecallRequest(query="朋友", level=self.level),
                ),
            )
        evaluation = (
            RecallEvaluation(
                usefulness="related",
                redundant=False,
                need_more=False,
                level_feedback="ok",
                note="够用",
            )
            if self.evaluate
            else None
        )
        return ModelResponse(
            text="最终回应",
            model=self.name,
            recall_evaluation=evaluation,
        )


class AlwaysRecallModel:
    name = "always-recall"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            text="还要更多",
            model=self.name,
            recall_requests=(RecallRequest(query="更多"),),
        )


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
    return process, repository


def subject_events(repository, event_type):
    return [
        record
        for record in repository.list_history("stone", HistoryKind.SUBJECT)
        if record.event_type == event_type
    ]


def test_single_recall_records_metrics_and_reference(tmp_path):
    model = FollowupModel(level=5)
    process, repository = runtime(tmp_path, model=model)
    seeded = process.memory.remember_fact(
        "stone", "external_input", "朋友上周很忙"
    )

    result = process.experience("stone", "他最近怎么样", object_ref="user")

    assert model.calls == 2
    assert result.thought.content == "最终回应"
    metrics = subject_events(repository, "recall_metrics")
    assert len(metrics) == 1
    content = metrics[0].content
    assert content["round"] == 1
    assert content["request"]["query"] == "朋友"
    assert content["request"]["level"] == 5
    assert content["duration_ms"] >= 0
    assert content["returned_count"] >= 1
    assert content["truncated"] is False
    assert seeded in metrics[0].source_ids

    references = subject_events(repository, "recall_reference")
    assert len(references) == 1
    assert references[0].content["recalled_event_ids"] == [seeded]
    assert references[0].content["referenced_ids"] == [seeded]
    assert references[0].content["rate"] == 1.0


def test_recall_is_truncated_after_one_round(tmp_path):
    model = AlwaysRecallModel()
    process, repository = runtime(tmp_path, model=model)

    result = process.experience("stone", "你好", object_ref="user")

    assert model.calls == 2  # 首次 + 追加后一次响应，不再执行第二轮
    assert result.thought.content == "还要更多"
    metrics = subject_events(repository, "recall_metrics")
    assert len(metrics) == 1
    assert metrics[0].content["truncated"] is True
    # 只执行了一次追加
    assert len(subject_events(repository, "recall_extended")) == 1


def test_recall_evaluation_recorded(tmp_path):
    model = FollowupModel(evaluate=True)
    process, repository = runtime(tmp_path, model=model)

    process.experience("stone", "他最近怎么样", object_ref="user")

    evaluated = subject_events(repository, "recall_evaluated")
    assert len(evaluated) == 1
    content = evaluated[0].content
    assert content["usefulness"] == "related"
    assert content["redundant"] is False
    assert content["level_feedback"] == "ok"
    assert content["note"] == "够用"
    metrics = subject_events(repository, "recall_metrics")
    assert evaluated[0].source_ids == (metrics[0].id,)


def test_recall_evaluation_missing_does_not_block(tmp_path):
    model = FollowupModel(evaluate=False)
    process, repository = runtime(tmp_path, model=model)

    result = process.experience("stone", "他最近怎么样", object_ref="user")

    assert result.thought.content == "最终回应"
    assert subject_events(repository, "recall_evaluated") == []
    assert len(subject_events(repository, "recall_metrics")) == 1


def test_no_recall_no_metrics(tmp_path):
    process, repository = runtime(tmp_path)

    process.experience("stone", "你好", object_ref="user")

    assert subject_events(repository, "recall_metrics") == []
    assert subject_events(repository, "recall_evaluated") == []
    assert subject_events(repository, "recall_reference") == []
