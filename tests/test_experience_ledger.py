from __future__ import annotations

from jshi.experienceledger import (
    ActorKind,
    ConsumerKind,
    InProcessExperienceLedger,
    OutputKind,
)
from jshi.memory import MemoryBatch


def test_append_external_sequence_and_head():
    ledger = InProcessExperienceLedger()
    first = ledger.append_external(
        "stone",
        actor_object_id="OBJ-A",
        text_raw="你好",
        source_ids=("fact-1",),
    )
    second = ledger.append_external(
        "stone",
        actor_object_id="OBJ-A",
        text_raw="继续",
        source_ids=("fact-2",),
    )

    assert first.sequence == 1
    assert second.sequence == 2
    assert ledger.head_sequence("stone") == 2
    assert first.output_kind == OutputKind.EXTERNAL_INPUT


def test_append_does_not_change_context_view_until_apply():
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="你好。")

    view = ledger.current_context_view("stone")
    assert view.version == 0
    assert view.context_text == ""
    assert view.segment_refs == ()


def test_apply_merges_pending_into_context_view():
    ledger = InProcessExperienceLedger()
    first = ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="你好。")
    reply = ledger.append_subject_reply("stone", text_raw="我在。")

    view = ledger.apply_context_assessment("stone")

    assert view.version == 1
    assert "你好。" in view.context_text
    assert "我在。" in view.context_text
    assert first.segment_id in view.segment_refs
    assert reply.segment_id in view.segment_refs
    assert view.last_applied_sequence == 2
    assert ledger.list_experiences("stone")[-1].segment_id == reply.segment_id


def test_context_view_carries_segment_texts_for_model_addressing():
    ledger = InProcessExperienceLedger()
    first = ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="第一句。")
    reply = ledger.append_subject_reply("stone", text_raw="我在。")

    view = ledger.apply_context_assessment("stone")

    assert view.segment_texts == (
        (first.segment_id, "第一句。"),
        (reply.segment_id, "我在。"),
    )
    assert view.context_text == "第一句。\n我在。"


def test_remove_drops_segment_and_merges_pending():
    from jshi.experienceledger import ContextAssessment

    ledger = InProcessExperienceLedger()
    first = ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="第一句。")
    second = ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="第二句。")
    third = ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="新来的。")

    view = ledger.apply_context_assessment(
        "stone",
        ContextAssessment(remove=(second.segment_id,)),
        allow_edit=True,
    )

    assert "第一句。" in view.context_text
    assert "第二句。" not in view.context_text
    assert "新来的。" in view.context_text
    assert first.segment_id in view.segment_refs
    assert second.segment_id not in view.segment_refs
    assert third.segment_id in view.segment_refs


def test_apply_keeps_recall_excerpts_and_speaker():
    from jshi.experienceledger import ContextAssessment

    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="你好。")
    view = ledger.apply_context_assessment(
        "stone",
        recall_excerpts=(("memory:e1", "上周很忙"),),
        speaker_object_id="OBJ-A",
    )
    assert view.speaker_object_id == "OBJ-A"
    assert view.recall_excerpts == (("memory:e1", "上周很忙"),)
    assert "上周很忙" in view.context_text

    trimmed = ledger.apply_context_assessment(
        "stone",
        ContextAssessment(drop_recall=("memory:e1",)),
        speaker_object_id="OBJ-A",
        protected_refs=("object:OBJ-A",),
    )
    assert trimmed.recall_excerpts == ()
    assert "上周很忙" not in trimmed.context_text
    assert trimmed.speaker_object_id == "OBJ-A"


def test_trim_does_not_drop_protected_object():
    from jshi.experienceledger import ContextAssessment

    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="你好。")
    view = ledger.apply_context_assessment(
        "stone",
        ContextAssessment(remove=("object:OBJ-A",)),
        speaker_object_id="OBJ-A",
        protected_refs=("object:OBJ-A",),
    )
    assert view.speaker_object_id == "OBJ-A"


def test_safe_trim_point_is_min_cursor():
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="two")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="three")
    ledger.advance_consumer_cursor(
        "stone", ConsumerKind.ACTIVE_ZONE, through_sequence=1
    )
    ledger.advance_consumer_cursor(
        "stone", ConsumerKind.MEMORY, through_sequence=2
    )

    assert ledger.safe_trim_point("stone") == 0

    ledger.advance_consumer_cursor(
        "stone", ConsumerKind.AUDIT, through_sequence=3
    )
    assert ledger.safe_trim_point("stone") == 1


def test_ingest_ledger_state_machine():
    ledger = InProcessExperienceLedger()
    batch = MemoryBatch(
        batch_id="batch-1",
        subject_id="stone",
        experiences=(),
        from_sequence=1,
        to_sequence=1,
    )

    entry = ledger.register_ingest(batch)
    assert entry.status == "pending"

    ingesting = ledger.mark_ingesting(entry.ingest_id)
    assert ingesting.status == "ingesting"

    ingested = ledger.mark_ingested(
        ingesting.ingest_id,
        memory_event_ids=("me-1",),
        stored_marks={"seg-1": ["me-1"]},
    )
    assert ingested.status == "ingested"
    assert ingested.memory_event_ids == ("me-1",)
    assert ingested.stored_marks == {"seg-1": ["me-1"]}


def test_subject_reply_is_appended_to_same_log():
    ledger = InProcessExperienceLedger()
    incoming = ledger.append_external(
        "stone",
        actor_object_id="OBJ-A",
        text_raw="你好",
    )
    reply = ledger.append_subject_reply(
        "stone",
        text_raw="我在。",
        source_ids=("activity-1",),
    )

    assert incoming.sequence == 1
    assert reply.sequence == 2
    assert reply.actor_kind == ActorKind.SUBJECT
    assert reply.output_kind == OutputKind.SUBJECT_REPLY
    assert reply.actor_object_id is None


def test_subject_state_without_text_is_logged():
    ledger = InProcessExperienceLedger()
    state = ledger.append_subject_state(
        "stone",
        state_delta={
            "from": "considering",
            "to": "accepted",
            "reason": "object_confirmed",
        },
        source_ids=("activity-1", "transition-1"),
    )

    assert state.actor_kind == ActorKind.SUBJECT
    assert state.output_kind == OutputKind.SUBJECT_STATE
    assert state.text_raw is None
    assert state.state_delta["to"] == "accepted"


def test_subject_silent_is_logged():
    ledger = InProcessExperienceLedger()
    silent = ledger.append_subject_silent(
        "stone",
        source_ids=("activity-1",),
    )

    assert silent.actor_kind == ActorKind.SUBJECT
    assert silent.output_kind == OutputKind.SUBJECT_SILENT
    assert silent.text_raw is None
    assert silent.state_delta is None


def test_segment_carries_objects_mapping():
    ledger = InProcessExperienceLedger()
    ledger.append_external(
        "stone",
        actor_object_id="OBJ-A",
        text_raw="小明：你好",
        objects={"小明": "OBJ-A", "小刚": "OBJ-B"},
    )
    segment = ledger.list_experiences("stone")[-1]
    assert segment.text_raw == "小明：你好"
    assert segment.objects == {"小明": "OBJ-A", "小刚": "OBJ-B"}


def test_sqlite_ledger_reopens_segments_and_active_zone(tmp_path):
    from jshi.experienceledger import SqliteExperienceLedger

    path = tmp_path / "subject.sqlite3"
    first = SqliteExperienceLedger(path)
    incoming = first.append_external(
        "stone", actor_object_id="OBJ-A", text_raw="上一句还在。"
    )
    reply = first.append_subject_reply("stone", text_raw="我接着说。")
    view = first.apply_context_assessment("stone", speaker_object_id="OBJ-A")
    first.advance_consumer_cursor("stone", ConsumerKind.MEMORY, incoming.sequence)

    second = SqliteExperienceLedger(path)
    restored = second.current_context_view("stone")
    assert restored.version == view.version
    assert restored.context_text == view.context_text
    assert restored.segment_refs == view.segment_refs
    assert restored.speaker_object_id == "OBJ-A"
    segments = second.list_experiences("stone")
    assert [item.segment_id for item in segments] == [
        incoming.segment_id,
        reply.segment_id,
    ]
    assert second.consumer_cursor("stone", ConsumerKind.MEMORY) == incoming.sequence
    pending = second.list_experiences("stone", after_sequence=incoming.sequence)
    assert [item.segment_id for item in pending] == [reply.segment_id]


def test_sqlite_unmerged_pending_survives_reopen(tmp_path):
    from jshi.experienceledger import SqliteExperienceLedger

    path = tmp_path / "subject.sqlite3"
    first = SqliteExperienceLedger(path)
    first.append_external("stone", actor_object_id="OBJ-A", text_raw="还没编进活跃区。")
    assert first.current_context_view("stone").version == 0

    second = SqliteExperienceLedger(path)
    assert second.current_context_view("stone").context_text == ""
    assert second.list_experiences("stone")[0].text_raw == "还没编进活跃区。"
    merged = second.apply_context_assessment("stone")
    assert "还没编进活跃区。" in merged.context_text


def _legacy_payload(ledger, subject_id):
    from jshi.experienceledger.sqlite import _context_to_dict, _ingest_to_dict, _segment_to_dict
    state = ledger._states[subject_id]
    return {
        "segments": [_segment_to_dict(s) for s in state.segments],
        "cursors": {k.value: v for k, v in state.cursors.items()},
        "ingest_entries": [_ingest_to_dict(e) for e in state.ingest_entries],
        "context": _context_to_dict(state.context),
        "pending_sequences": list(state.pending_sequences),
    }


def test_sqlite_ledger_migrates_whole_payload_to_rows(tmp_path):
    import json
    import sqlite3
    from jshi.experienceledger import InProcessExperienceLedger, SqliteExperienceLedger

    source = InProcessExperienceLedger()
    first = source.append_external("stone", actor_object_id="OBJ-A", text_raw="旧格式第一段。")
    second = source.append_subject_reply("stone", text_raw="旧格式第二段。")
    view = source.apply_context_assessment("stone", speaker_object_id="OBJ-A")
    source.advance_consumer_cursor("stone", ConsumerKind.MEMORY, first.sequence)
    entry = source.register_ingest(MemoryBatch(batch_id="B1", subject_id="stone", experiences=(),
                                               from_sequence=1, to_sequence=2))
    source.mark_ingested(entry.ingest_id, memory_event_ids=("E1",))
    path = tmp_path / "subject.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE experience_ledger (subject_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("INSERT INTO experience_ledger VALUES (?, ?)",
                     ("stone", json.dumps(_legacy_payload(source, "stone"), ensure_ascii=False)))

    ledger = SqliteExperienceLedger(path)
    assert [s.segment_id for s in ledger.list_experiences("stone")] == [first.segment_id, second.segment_id]
    assert ledger.current_context_view("stone") == view
    assert ledger.consumer_cursor("stone", ConsumerKind.MEMORY) == first.sequence
    assert ledger.list_ingest_entries("stone")[0].memory_event_ids == ("E1",)
    with sqlite3.connect(path) as conn:
        meta = json.loads(conn.execute("SELECT payload FROM experience_ledger").fetchone()[0])
        assert "segments" not in meta and "ingest_entries" not in meta
        assert conn.execute("SELECT count(*) FROM experience_segments").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM experience_ingest").fetchone()[0] == 1

    reopened = SqliteExperienceLedger(path)
    assert [s.text_raw for s in reopened.list_experiences("stone")] == ["旧格式第一段。", "旧格式第二段。"]
    assert reopened.current_context_view("stone") == view


def test_sqlite_ledger_append_writes_only_the_new_row(tmp_path, monkeypatch):
    from jshi.experienceledger import SqliteExperienceLedger

    ledger = SqliteExperienceLedger(tmp_path / "subject.sqlite3")
    for index in range(5):
        ledger.append_external("stone", actor_object_id="OBJ-A", text_raw=f"第{index}段")
    written = []
    original = SqliteExperienceLedger._write
    def spy(connection, subject_id, state, *, segments, ingest):
        written.append([s.sequence for s in segments])
        return original(connection, subject_id, state, segments=segments, ingest=ingest)
    monkeypatch.setattr(SqliteExperienceLedger, "_write", staticmethod(spy))
    ledger.append_subject_reply("stone", text_raw="新一段")
    ledger.advance_consumer_cursor("stone", ConsumerKind.MEMORY, 3)
    assert written == [[6], []]


def test_sqlite_ledger_failed_write_retries_the_row_next_time(tmp_path, monkeypatch):
    import pytest
    from jshi.experienceledger import SqliteExperienceLedger

    path = tmp_path / "subject.sqlite3"
    ledger = SqliteExperienceLedger(path)
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="先写入")
    original = SqliteExperienceLedger._write
    def fail(*args, **kwargs):
        raise OSError("disk")
    monkeypatch.setattr(SqliteExperienceLedger, "_write", staticmethod(fail))
    with pytest.raises(OSError):
        ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="写入失败的一段")
    monkeypatch.setattr(SqliteExperienceLedger, "_write", staticmethod(original))
    ledger.advance_consumer_cursor("stone", ConsumerKind.MEMORY, 1)
    reopened = SqliteExperienceLedger(path)
    assert [s.text_raw for s in reopened.list_experiences("stone")] == ["先写入", "写入失败的一段"]

