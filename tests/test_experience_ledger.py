from __future__ import annotations

from jshi.experienceledger import (
    ActorKind,
    ConsumerKind,
    InProcessExperienceLedger,
    OutputKind,
)


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


def test_trim_applies_to_view_the_model_saw_then_merges_pending():
    from jshi.experienceledger import ContextAssessment

    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="第一句。第二句。")
    ledger.apply_context_assessment("stone")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="新来的。")

    view = ledger.apply_context_assessment(
        "stone",
        ContextAssessment(need_trim=True, trim_refs=("2",)),
        allow_edit=True,
    )

    assert "第一句。" in view.context_text
    assert "第二句。" not in view.context_text
    assert "新来的。" in view.context_text
    assert view.excluded_sentence_refs


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
        ContextAssessment(need_trim=True, trim_refs=("memory:e1",)),
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
        ContextAssessment(need_trim=True, trim_refs=("object:OBJ-A",)),
        speaker_object_id="OBJ-A",
        protected_refs=("object:OBJ-A",),
    )
    assert view.speaker_object_id == "OBJ-A"


def test_memory_batch_does_not_change_context_view():
    ledger = InProcessExperienceLedger(memory_batch_segments=1)
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="你好。")
    before = ledger.apply_context_assessment("stone")

    batch = ledger.build_memory_batch("stone")
    assert batch is not None
    after = ledger.current_context_view("stone")

    assert after == before


def test_memory_batch_not_built_below_threshold():
    ledger = InProcessExperienceLedger(
        memory_batch_chars=1000,
        memory_batch_segments=3,
    )
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="a")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="b")

    assert ledger.build_memory_batch("stone") is None


def test_memory_batch_covers_only_new_segments():
    ledger = InProcessExperienceLedger(
        memory_batch_chars=1000,
        memory_batch_segments=2,
    )
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="two")

    batch = ledger.build_memory_batch("stone")

    assert batch is not None
    assert batch.from_sequence == 1
    assert batch.to_sequence == 2
    assert ledger.consumer_cursor("stone", ConsumerKind.MEMORY) == 0


def test_memory_batch_advance_then_next_batch_is_incremental():
    ledger = InProcessExperienceLedger(
        memory_batch_chars=1000,
        memory_batch_segments=2,
    )
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="two")
    first = ledger.build_memory_batch("stone")
    assert first is not None

    ledger.advance_consumer_cursor(
        "stone", ConsumerKind.MEMORY, through_sequence=first.to_sequence
    )
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="three")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="four")

    second = ledger.build_memory_batch("stone")

    assert second is not None
    assert second.from_sequence == 3
    assert second.to_sequence == 4


def test_object_ids_are_external_plus_mentioned_not_subject():
    ledger = InProcessExperienceLedger(
        memory_batch_chars=10,
        memory_batch_segments=1,
    )
    ledger.append_external(
        "stone",
        actor_object_id="OBJ-A",
        mentioned_object_ids=("OBJ-B",),
        text_raw="他提到你",
    )

    batch = ledger.build_memory_batch("stone")

    assert batch is not None
    assert batch.object_ids == ("OBJ-A", "OBJ-B")
    assert "stone" not in batch.object_ids


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
    ledger = InProcessExperienceLedger(memory_batch_segments=1)
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    batch = ledger.build_memory_batch("stone")
    assert batch is not None

    entry = ledger.register_ingest(batch)
    assert entry.status == "pending"

    ingesting = ledger.mark_ingesting(entry.ingest_id)
    assert ingesting.status == "ingesting"

    ingested = ledger.mark_ingested(
        ingesting.ingest_id,
        memory_event_ids=("me-1",),
        stored_marks={"event-1": "me-1"},
    )
    assert ingested.status == "ingested"
    assert ingested.memory_event_ids == ("me-1",)
    assert ingested.stored_marks == {"event-1": "me-1"}


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


def test_memory_batch_includes_reply_state_and_keeps_subject_out_of_objects():
    ledger = InProcessExperienceLedger(
        memory_batch_chars=10,
        memory_batch_segments=3,
    )
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="你好")
    ledger.append_subject_reply(
        "stone",
        text_raw="我在。",
        mentioned_object_ids=("OBJ-B",),
    )
    ledger.append_subject_state(
        "stone",
        state_delta={"from": "considering", "to": "accepted"},
    )

    batch = ledger.build_memory_batch("stone")

    assert batch is not None
    assert batch.from_sequence == 1
    assert batch.to_sequence == 3
    assert batch.object_ids == ("OBJ-A", "OBJ-B")
    assert "stone" not in batch.object_ids
    assert [segment.output_kind for segment in batch.segments] == [
        OutputKind.EXTERNAL_INPUT,
        OutputKind.SUBJECT_REPLY,
        OutputKind.SUBJECT_STATE,
    ]
