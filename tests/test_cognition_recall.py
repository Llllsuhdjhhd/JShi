"""05 认知与上下文统筹测试。

覆盖：05 单次认知产出、主流程编排补充召回、记忆侧指标由 09 协调器写、
独立评价过程被触发、评价不可用不阻塞。
"""

from __future__ import annotations

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import (
    ModelRequest,
    ModelResponse,
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

    def __init__(self, *, level: int = 1) -> None:
        self.calls = 0
        self.level = level

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
        return ModelResponse(text="最终回应", model=self.name)


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
    process = SubjectProcess(
        repository,
        identities,
        model or FixedModel(),
    )
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
    assert result.action_text == "最终回应"
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
    # 一条初始召回（按对象过滤）拿不到、但追加召回（不按对象过滤）能拿到的新经历：
    # 验证追加召回能补进"真新"记忆，且与初始召回按 event_id 去重（不会重复装已有记忆）。
    seeded = process.memory.remember_fact("stone", "external_input", "更多往事")

    result = process.experience("stone", "你好", object_ref="user")

    assert model.calls == 2
    assert result.action_text == "还要更多"
    metrics = subject_events(repository, "recall_metrics")
    assert len(metrics) == 1
    assert metrics[0].content["truncated"] is True
    extended = subject_events(repository, "recall_extended")
    assert len(extended) == 1
    # 追加召回只装入"真新"的那条（seeded），不会重复装初始召回已装的那条经历。
    assert extended[0].content["recalled_event_ids"] == [seeded]


def test_evaluation_is_not_run_inline(tmp_path):
    model = FollowupModel()
    process, repository = runtime(tmp_path, model=model)

    process.experience("stone", "他最近怎么样", object_ref="user")

    assert subject_events(repository, "recall_evaluated") == []
    assert len(subject_events(repository, "recall_metrics")) == 1


def test_model_request_carries_speaker_and_addressable_context(tmp_path):
    class CaptureModel:
        name = "capture-model"

        def __init__(self) -> None:
            self.request = None

        def generate(self, request: ModelRequest):
            self.request = request
            return ModelResponse(text="回应", model=self.name)

    model = CaptureModel()
    process, _repository = runtime(tmp_path, model=model)
    process.experience("stone", "你好", object_ref="user")

    assert model.request is not None
    assert model.request.speaker is not None
    assert model.request.speaker.label == "user"
    assert model.request.speaker.object_id
    assert all("id" in item and "source" in item for item in model.request.context)
    assert any(item.get("kind") == "speaker" for item in model.request.context)
    zone_refs = next(
        item for item in model.request.context if item.get("kind") == "active_zone_refs"
    )
    assert "segments" in zone_refs


def test_no_recall_no_metrics_or_evaluation(tmp_path):
    process, repository = runtime(tmp_path)

    process.experience("stone", "你好", object_ref="user")

    assert subject_events(repository, "recall_metrics") == []
    assert subject_events(repository, "recall_evaluated") == []
    assert subject_events(repository, "recall_reference") == []


def test_trim_plan_updates_active_zone_without_second_model_call(tmp_path):
    from jshi.experienceledger import ContextAssessment

    class TrimModel:
        name = "trim-model"

        def __init__(self) -> None:
            self.calls = 0
            self.seen_ids: list[str] = []

        def generate(self, request: ModelRequest):
            self.calls += 1
            self.seen_ids = [str(item.get("id", "")) for item in request.context]
            return ModelResponse(
                text="回应",
                model=self.name,
                context_assessment=ContextAssessment(
                    drop_recall=("memory:kept-out",),
                ),
            )

    model = TrimModel()
    process, _repository = runtime(tmp_path, model=model)
    process.activity_ledger.apply_context_assessment(
        "stone",
        recall_excerpts=(("memory:kept-out", "过时线索"),),
        speaker_object_id="OBJ-USER",
    )
    process.experience("stone", "你好", object_ref="user")
    view = process.activity_ledger.current_context_view("stone")

    assert model.calls == 1
    assert view.recall_excerpts == ()
    assert "过时线索" not in view.context_text
    assert view.speaker_object_id
