from __future__ import annotations

from datetime import datetime, timezone

from jshi.app import cli
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.memory import InProcessHistoryMemory, RecalledFragment, recall_level_limit
from jshi.models import ModelRequest, ModelResponse
from jshi.recognition import ObjectProfile
from jshi.subject import (
    HistoryKind,
    HistoryRecord,
    SubjectProcess,
    SubjectRepository,
)


class FixedModel:
    name = "fixed-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="响应", model=self.name)


def test_recall_filters_by_object_id(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    memory = InProcessHistoryMemory(repository)
    repository.add_history(
        HistoryRecord(
            subject_id="stone",
            kind=HistoryKind.FACT,
            event_type="external_input",
            content={"text": "对象 A 说过朋友最近很忙", "object_id": "OBJ-A"},
        )
    )
    repository.add_history(
        HistoryRecord(
            subject_id="stone",
            kind=HistoryKind.FACT,
            event_type="external_input",
            content={"text": "对象 B 说过朋友最近休假", "object_id": "OBJ-B"},
        )
    )

    recalled = memory.recall("stone", "朋友最近", object_id="OBJ-A")

    assert [item.event_id for item in recalled] == [
        record.id
        for record in repository.list_history("stone", HistoryKind.FACT)
        if record.content.get("object_id") == "OBJ-A"
    ]
    assert recalled[0].object_id == "OBJ-A"


def test_recall_uses_chinese_bigrams_over_recency_fallback(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    memory = InProcessHistoryMemory(repository)
    repository.add_history(
        HistoryRecord(
            subject_id="stone",
            kind=HistoryKind.FACT,
            event_type="external_input",
            content={"text": "朋友上周很忙"},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    repository.add_history(
        HistoryRecord(
            subject_id="stone",
            kind=HistoryKind.FACT,
            event_type="external_input",
            content={"text": "今天天气不错"},
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
    )

    recalled = memory.recall("stone", "朋友最近怎么样", limit=2)

    assert recalled[0].text == "朋友上周很忙"


def test_subject_process_wires_injected_memory_into_assembly(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石，从共同基础出发。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")

    class FixedMemory:
        def recall(
            self,
            subject_id,
            query,
            *,
            limit=None,
            object_id=None,
            level=1,
            anchor_event_ids=(),
        ):
            return (
                RecalledFragment(
                    event_id="memory-injected",
                    event_type="external_input",
                    text="injected-memory",
                    kind="fact",
                    object_id=object_id,
                ),
            )

    process = SubjectProcess(
        repository,
        identities,
        FixedModel(),
        memory=FixedMemory(),
    )
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="user", source="test", status="confirmed"
        )
    )

    preview = process.preview_state("stone", "你好", object_ref="user")

    memory_fragments = [
        fragment
        for fragment in preview.assembled.fragments
        if fragment.source == "memory"
    ]
    assert [fragment.id for fragment in memory_fragments] == [
        "memory:memory-injected"
    ]
    assert memory_fragments[0].content == "injected-memory"
    assert memory_fragments[0].object_id == "OBJ-USER"


def test_cli_recall_uses_memory_port(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("JSHI_MODEL_ENDPOINT", raising=False)
    monkeypatch.delenv("JSHI_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("JSHI_MODEL_NAME", raising=False)
    data_dir = tmp_path / "cli-data"

    monkeypatch.setattr(
        "sys.argv", ["jshi", "--data-dir", str(data_dir), "create", "stone"]
    )
    cli.main()
    monkeypatch.setattr(
        "sys.argv",
        [
            "jshi",
            "--data-dir",
            str(data_dir),
            "experience",
            "stone",
            "你好",
            "--speaker",
            "user",
        ],
    )
    cli.main()
    capsys.readouterr()

    monkeypatch.setattr(
        "sys.argv",
        [
            "jshi",
            "--data-dir",
            str(data_dir),
            "recall",
            "stone",
            "你好",
        ],
    )
    cli.main()

    assert "external_input" in capsys.readouterr().out


def test_recall_level_limit_mapping():
    assert recall_level_limit(1) == 3
    assert recall_level_limit(3) == 3
    assert recall_level_limit(4) == 5
    assert recall_level_limit(6) == 5
    assert recall_level_limit(7) == 8
    assert recall_level_limit(9) == 8
    assert recall_level_limit(0) == 3
    assert recall_level_limit(10) == 8


def test_recall_default_limit_follows_level(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    memory = InProcessHistoryMemory(repository)
    for index in range(8):
        repository.add_history(
            HistoryRecord(
                subject_id="stone",
                kind=HistoryKind.FACT,
                event_type="external_input",
                content={"text": f"朋友的第{index}条"},
            )
        )

    assert len(memory.recall("stone", "朋友")) == 3  # 默认档位 1
    assert len(memory.recall("stone", "朋友", level=5)) == 5
    assert len(memory.recall("stone", "朋友", level=9)) == 8


def test_recall_explicit_limit_overrides_level(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    memory = InProcessHistoryMemory(repository)
    for index in range(8):
        repository.add_history(
            HistoryRecord(
                subject_id="stone",
                kind=HistoryKind.FACT,
                event_type="external_input",
                content={"text": f"朋友的第{index}条"},
            )
        )

    assert len(memory.recall("stone", "朋友", limit=2, level=9)) == 2


def test_recall_carries_occurred_at(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    memory = InProcessHistoryMemory(repository)
    occurred = datetime(2026, 8, 29, 20, 15, tzinfo=timezone.utc)
    repository.add_history(
        HistoryRecord(
            subject_id="stone",
            kind=HistoryKind.FACT,
            event_type="memory_external",
            content={"text": "昨天的事", "occurred_at": occurred.isoformat()},
        )
    )

    recalled = memory.recall("stone", "昨天")

    assert recalled[0].occurred_at == occurred


def test_recall_falls_back_to_created_at_when_no_occurred_at(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    memory = InProcessHistoryMemory(repository)
    record = HistoryRecord(
        subject_id="stone",
        kind=HistoryKind.FACT,
        event_type="external_input",
        content={"text": "过去的事"},
        created_at=datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
    )
    repository.add_history(record)

    recalled = memory.recall("stone", "过去")

    assert recalled[0].occurred_at == record.created_at
