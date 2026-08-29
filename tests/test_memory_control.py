from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from jshi.experienceledger import ConsumerKind, InProcessExperienceLedger
from jshi.memory import BackendIngestResult
from jshi.memorycontrol import InProcessMemoryControl


def _at(day: int = 1, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


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
        return BackendIngestResult(
            subject_id=batch.subject_id,
            stored_marks={
                experience.segment_id: [f"me-{batch.to_sequence}"]
                for experience in batch.experiences
                if experience.segment_id
            },
            sealed_event_ids=(f"me-{batch.to_sequence}",),
        )


class BlockingMemory(RecordingMemory):
    """ingest_batch 会阻塞，直到 release 被置位；用于验证异步投递不阻塞调用线程。"""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def ingest_batch(self, batch):
        self.release.wait(timeout=5)
        return super().ingest_batch(batch)


def test_insufficient_data_is_skipped():
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    memory = RecordingMemory()
    control = InProcessMemoryControl(
        ledger, memory, flush_max_segments=2, now=lambda: _at(1, 12, 0)
    )

    decision = control.evaluate("stone")
    result = control.run_once("stone")

    assert decision.should_trigger is False
    assert decision.reason == "insufficient_memory_data"
    assert result.status == "skipped"
    assert memory.calls == 0


def test_success_advances_memory_cursor():
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="two")
    memory = RecordingMemory()
    control = InProcessMemoryControl(ledger, memory, flush_max_segments=2)

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
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="two")
    memory = RecordingMemory(fail_times=1)
    control = InProcessMemoryControl(ledger, memory, flush_max_segments=2)

    failed = control.run_once("stone")

    assert failed.status == "failed"
    assert ledger.consumer_cursor("stone", ConsumerKind.MEMORY) == 0

    retry_decision = control.evaluate("stone")
    assert retry_decision.should_trigger is True
    retried = control.run_once("stone")
    assert retried.status == "ingested"
    assert ledger.consumer_cursor("stone", ConsumerKind.MEMORY) == 2


def test_max_retry_blocks_new_trigger():
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    memory = RecordingMemory(fail_times=10)
    control = InProcessMemoryControl(ledger, memory, max_retry=2, flush_max_segments=1)

    assert control.run_once("stone").status == "failed"
    assert control.run_once("stone").status == "failed"

    decision = control.evaluate("stone")
    assert decision.should_trigger is False
    assert decision.reason == "max_retry_reached"


def test_flush_by_char_count():
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="a" * 1500)
    control = InProcessMemoryControl(
        ledger, RecordingMemory(), flush_max_chars=2000, now=lambda: _at(1, 12, 0)
    )

    assert control.evaluate("stone").should_trigger is False

    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="b" * 600)
    decision = control.evaluate("stone")
    assert decision.should_trigger is True
    assert decision.reason == "flush_max_chars"


def test_run_async_returns_without_waiting_and_drain_settles():
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="two")
    memory = BlockingMemory()
    control = InProcessMemoryControl(ledger, memory, flush_max_segments=2)

    result = control.run_async("stone")

    assert result.status == "ingesting"
    assert result.ingest_id
    # 后台 ingest 仍在阻塞时，drain 不能等待、不能阻塞调用线程。
    assert control.drain("stone") is None
    assert ledger.consumer_cursor("stone", ConsumerKind.MEMORY) == 0

    memory.release.set()
    settled = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        settled = control.drain("stone")
        if settled is not None:
            break
        time.sleep(0.005)

    assert settled is not None
    assert settled.status == "ingested"
    assert ledger.consumer_cursor("stone", ConsumerKind.MEMORY) == 2


def test_flush_on_idle_uses_newest_segment_time():
    ledger = InProcessExperienceLedger()
    first = ledger.append_external(
        "stone", actor_object_id="OBJ-A", text_raw="one", occurred_at=_at(1, 0, 0)
    )
    ledger.append_external(
        "stone",
        actor_object_id="OBJ-A",
        text_raw="two",
        occurred_at=_at(1, 0, 10),
    )
    memory = RecordingMemory()
    control = InProcessMemoryControl(
        ledger,
        memory,
        flush_on_idle_seconds=3600,
        flush_max_idle_seconds=3600,
        now=lambda: _at(2, 2, 0),
    )

    decision = control.evaluate("stone")
    assert decision.should_trigger is True
    assert decision.reason == "flush_on_idle"


def test_flush_max_idle_since_last_flush():
    ledger = InProcessExperienceLedger()
    ledger.append_external(
        "stone",
        actor_object_id="OBJ-A",
        text_raw="one",
        occurred_at=_at(1, 11, 0),
    )
    memory = RecordingMemory()
    now = _at(1, 12, 0)
    control = InProcessMemoryControl(
        ledger,
        memory,
        flush_max_segments=2,
        flush_max_idle_seconds=600,
        flush_on_idle_seconds=3600,
        now=lambda: now,
    )

    # 首段较旧 → 空闲触发首次冲刷
    assert control.evaluate("stone").reason == "flush_on_idle"
    assert control.run_once("stone").status == "ingested"

    ledger.append_external(
        "stone",
        actor_object_id="OBJ-A",
        text_raw="two",
        occurred_at=_at(1, 12, 1),
    )
    now = _at(1, 12, 5)
    assert control.evaluate("stone").reason == "insufficient_memory_data"

    now = _at(1, 12, 11)  # 距上次冲刷 11 分钟 > 600 秒
    decision = control.evaluate("stone")
    assert decision.should_trigger is True
    assert decision.reason == "flush_max_idle"


def test_flush_night_window_triggers_all_pending():
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    control = InProcessMemoryControl(
        ledger,
        RecordingMemory(),
        flush_night_window="00:00-06:00",
        now=lambda: _at(1, 3, 30),
    )

    decision = control.evaluate("stone")
    assert decision.should_trigger is True
    assert decision.reason == "flush_night_window"


def test_night_window_not_active_outside_window():
    ledger = InProcessExperienceLedger()
    ledger.append_external("stone", actor_object_id="OBJ-A", text_raw="one")
    control = InProcessMemoryControl(
        ledger,
        RecordingMemory(),
        flush_night_window="00:00-06:00",
        now=lambda: _at(1, 12, 0),
    )

    assert control.evaluate("stone").should_trigger is False
