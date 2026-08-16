from __future__ import annotations

from jshi.activityledger import InProcessActivityLedger
from jshi.memory import InProcessMemoryBackend, MemoryShell
from jshi.subject import HistoryKind, SubjectRepository


def test_memory_shell_ingests_batch_without_creating_objects(tmp_path):
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    backend = InProcessMemoryBackend(repository)
    shell = MemoryShell(backend)
    ledger = InProcessActivityLedger(memory_batch_segments=2)
    ledger.append_external(
        "stone",
        actor_object_id="OBJ-A",
        text_raw="你好",
    )
    ledger.append_subject_reply(
        "stone",
        text_raw="我在。",
    )
    batch = ledger.build_memory_batch("stone")
    assert batch is not None

    result = shell.ingest_batch(batch)

    assert result.consumed_through_sequence == 2
    assert len(result.memory_event_ids) == 2
    assert len(result.stored_marks) == 2

    facts = repository.list_history("stone", HistoryKind.FACT)
    assert any(record.event_type == "memory_external_input" for record in facts)
    assert any(record.event_type == "memory_subject_reply" for record in facts)


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
