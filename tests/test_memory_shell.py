from __future__ import annotations

from jshi.experienceledger import ConsumerKind, InProcessExperienceLedger
from jshi.memory import InProcessMemoryBackend, MemoryShell
from jshi.memorycontrol import InProcessMemoryControl
from jshi.subject import HistoryKind, SubjectRepository


def test_memory_shell_ingests_batch_without_creating_objects(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    backend = InProcessMemoryBackend(repository)
    shell = MemoryShell(backend)
    ledger = InProcessExperienceLedger()
    ledger.append_external(
        "stone",
        actor_object_id="OBJ-A",
        text_raw="你好",
    )
    ledger.append_subject_reply(
        "stone",
        text_raw="我在。",
    )
    control = InProcessMemoryControl(ledger, shell, flush_max_segments=2)

    result = control.run_once("stone")

    assert result.status == "ingested"
    assert ledger.consumer_cursor("stone", ConsumerKind.MEMORY) == 2

    facts = repository.list_history("stone", HistoryKind.FACT)
    memory_events = [
        record for record in facts if record.event_type == "memory_external"
    ]
    assert len(memory_events) == 2


def test_memory_shell_recall_uses_object_filter_from_existing_facts(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    backend = InProcessMemoryBackend(repository)
    shell = MemoryShell(backend)

    shell.remember_fact(
        "stone",
        "external_input",
        "OBJ-A 说过朋友很忙",
    )
    shell.remember_fact(
        "stone",
        "external_input",
        "OBJ-B 说过天气不错",
    )

    recalled = shell.recall("stone", "朋友", object_id=None)

    assert recalled
    assert "朋友" in recalled[0].text


def test_memory_shell_ingest_keeps_raw_text_and_object_ids(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    backend = InProcessMemoryBackend(repository)
    shell = MemoryShell(backend)
    ledger = InProcessExperienceLedger()
    ledger.append_external(
        "stone",
        actor_object_id="OBJ-A",
        text_raw="宝玉笑道：这个妹妹我曾见过的。",
        objects={"宝玉": "OBJ-BAO", "贾母": "OBJ-JIA"},
    )
    control = InProcessMemoryControl(ledger, shell, flush_max_segments=1)

    result = control.run_once("stone")

    assert result.status == "ingested"
    facts = repository.list_history("stone", HistoryKind.FACT)
    memory = next(
        item for item in facts if item.event_type == "memory_external"
    )
    assert memory.content["text"] == "宝玉笑道：这个妹妹我曾见过的。"
    assert "OBJ-BAO" in memory.content["object_ids"]
    assert "OBJ-JIA" in memory.content["object_ids"]
