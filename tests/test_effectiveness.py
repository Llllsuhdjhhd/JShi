"""有效性分析：打分入库、规则调档、失败不挡说话、gap 下一拍取第三人。"""

from __future__ import annotations

from jshi.effectiveness import InProcessEffectiveness, JsonlRatingStore
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.memory import RecallStrategyStore
from jshi.models import MemoryRating, MemoryRatings, ModelRequest, ModelResponse
from jshi.recognition import ObjectProfile
from jshi.subject import HistoryKind, HistoryRecord, SubjectProcess, SubjectRepository


class FixedModel:
    name = "fixed-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="回应", model=self.name)


class RatingModel:
    name = "rating-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="回应",
            model=self.name,
            memory_ratings=MemoryRatings(
                items=(
                    MemoryRating(
                        ref="memory:a",
                        relevance="related",
                        helps_understanding=1,
                    ),
                ),
                coverage="thin",
                gap_query="lux 岭南",
            ),
        )


def runtime(tmp_path, model=None, *, effectiveness=None, recall_strategy=None):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(
        repository,
        identities,
        model or FixedModel(),
        recall_strategy=recall_strategy,
        effectiveness=effectiveness,
    )
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="user", source="test", status="confirmed"
        )
    )
    return process, repository


def test_ratings_are_written_to_jsonl(tmp_path):
    path = tmp_path / "memory_ratings.jsonl"
    store = JsonlRatingStore(path)
    effectiveness = InProcessEffectiveness(ratings=store)
    process, _repository = runtime(
        tmp_path, model=RatingModel(), effectiveness=effectiveness
    )

    process.experience("stone", "你好", object_ref="user")

    text = path.read_text(encoding="utf-8")
    assert "gap_query" in text
    assert "lux 岭南" in text
    rows = store.list("stone")
    assert len(rows) == 1
    assert rows[0].coverage == "thin"
    assert rows[0].items[0]["ref"] == "memory:a"


def test_analyzer_lowers_level_on_unrelated(tmp_path):
    strategy = RecallStrategyStore(tmp_path / "recall_strategy.json")
    strategy.apply("stone", default_level=2)
    effectiveness = InProcessEffectiveness(strategy=strategy)
    ratings = MemoryRatings(
        items=(MemoryRating(ref="memory:x", relevance="unrelated"),),
        coverage="sufficient",
    )
    for index in range(8):
        effectiveness.record_ratings("stone", f"act-{index}", ratings)

    effectiveness.run_due("stone")

    assert strategy.get("stone").default_level == 1
    assert strategy.get("stone").source_report_id
    reports = effectiveness.reports("stone")
    assert len(reports) == 1
    assert reports[0].analyzer == "memory_quality"
    assert reports[0].model_tag == ""


def test_run_due_failure_does_not_block_speech(tmp_path):
    class Boom:
        def pending_gap_query(self, subject_id):
            return ""

        def consume_gap(self, subject_id):
            return None

        def record_ratings(self, *args, **kwargs):
            return None

        def run_due(self, subject_id):
            raise RuntimeError("boom")

    process, _repository = runtime(tmp_path)
    process.effectiveness = Boom()
    result = process.experience("stone", "你好", object_ref="user")
    assert result.action_text == "回应"


def test_gap_query_loads_third_person_next_turn(tmp_path):
    class TwoTurnModel:
        name = "gap-model"

        def __init__(self) -> None:
            self.calls = 0
            self.second_contents: tuple[str, ...] = ()

        def generate(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    text="先记下",
                    model=self.name,
                    memory_ratings=MemoryRatings(
                        coverage="missing",
                        gap_query="lux 岭南",
                    ),
                )
            self.second_contents = tuple(
                str(item.get("content") or "") for item in request.context
            )
            return ModelResponse(text="第二拍", model=self.name)

    model = TwoTurnModel()
    process, repository = runtime(tmp_path, model=model)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-LUX", label="lux", source="test", status="confirmed"
        )
    )
    repository.add_history(
        HistoryRecord(
            subject_id="stone",
            kind=HistoryKind.FACT,
            event_type="memory_external",
            content={
                "text": "lux 住在岭南。",
                "object_id": "OBJ-LUX",
                "object_ids": ["OBJ-LUX"],
            },
        )
    )

    process.experience("stone", "lux 是不是你朋友", object_ref="user")
    process.experience("stone", "刚才那事", object_ref="user")

    assert model.calls == 2
    assert any("岭南" in text for item in model.second_contents for text in (item,))
