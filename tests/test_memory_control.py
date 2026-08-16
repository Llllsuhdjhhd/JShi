from __future__ import annotations

from jshi.experienceledger import ConsumerKind, InProcessExperienceLedger
from jshi.memorycontrol import (
    InProcessMemoryControl,
    MemoryIngestResult,
)


class RecordingMemory:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times
        self.batches = []

    def ingest_batch(self, batch):
        self.calls += 1
        self.batches.append(batch)
        if self.calls <= self.fail_times:
            raise RuntimeError("backend failed")
        return MemoryIngestResult(
            consumed_through_sequence=batch.to_sequence,
            memory_event_ids=(f"me-{batch.to_sequence}",),
            stored_marks={},
        )


def test_insufficient_data_is_skipped():
    ledger = InProcessExperienceLedger(memory_batch_segments=2)
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    memory = RecordingMemory()
    control = InProcessMemoryControl(ledger, memory)

    decision = control.evaluate("stone")
    result = control.run_once("stone")

    assert decision.should_trigger is False
    assert decision.reason == "insufficient_memory_data"
    assert result.status == "skipped"
    assert memory.calls == 0


def test_success_advances_memory_cursor():
    ledger = InProcessExperienceLedger(memory_batch_segments=2)
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="two")
    memory = RecordingMemory()
    control = InProcessMemoryControl(ledger, memory)

    result = control.run_once("stone")

    assert result.status == "ingested"
    assert memory.calls == 1
    assert ledger.consumer_cursor("stone", ConsumerKind.MEMORY) == 2

    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="three")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="four")
    second = control.run_once("stone")
    assert second.status == "ingested"
    assert memory.batches[-1].from_sequence == 3
    assert memory.batches[-1].to_sequence == 4


def test_failure_is_retryable_and_does_not_advance_cursor():
    ledger = InProcessExperienceLedger(memory_batch_segments=2)
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="two")
    memory = RecordingMemory(fail_times=1)
    control = InProcessMemoryControl(ledger, memory)

    failed = control.run_once("stone")

    assert failed.status == "failed"
    assert ledger.consumer_cursor("stone", ConsumerKind.MEMORY) == 0

    retry_decision = control.evaluate("stone")
    assert retry_decision.should_trigger is True
    retried = control.run_once("stone")
    assert retried.status == "ingested"
    assert ledger.consumer_cursor("stone", ConsumerKind.MEMORY) == 2


def test_max_retry_blocks_new_trigger():
    ledger = InProcessExperienceLedger(memory_batch_segments=1)
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    memory = RecordingMemory(fail_times=10)
    control = InProcessMemoryControl(ledger, memory, max_retry=2)

    assert control.run_once("stone").status == "failed"
    assert control.run_once("stone").status == "failed"

    decision = control.evaluate("stone")
    assert decision.should_trigger is False
    assert decision.reason == "max_retry_reached"
