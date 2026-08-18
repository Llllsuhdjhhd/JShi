from __future__ import annotations

from dataclasses import dataclass, field

from jshi.experienceledger import (
    ExperienceLedgerPort,
    ConsumerKind,
    MemoryBatch,
)

from .port import (
    MemoryBatchIngestPort,
    MemoryControlPort,
    MemoryControlResult,
    MemoryIngestResult,
    MemoryProcessStatus,
    MemoryTriggerDecision,
)


@dataclass
class _ControlState:
    previous_status: str = "idle"
    attempts: int = 0
    last_error: str = ""


class InProcessMemoryControl(MemoryControlPort):
    """07 最小实现：只管理投递决策、游标、重试和台账。"""

    def __init__(
        self,
        ledger: ExperienceLedgerPort,
        memory: MemoryBatchIngestPort,
        *,
        max_retry: int = 3,
        min_chars: int | None = None,
        min_segments: int | None = None,
        max_age_seconds: float | None = None,
    ) -> None:
        self._ledger = ledger
        self._memory = memory
        self._max_retry = max_retry
        self._min_chars = min_chars
        self._min_segments = min_segments
        self._max_age_seconds = max_age_seconds
        self._states: dict[str, _ControlState] = {}

    def _state(self, subject_id: str) -> _ControlState:
        state = self._states.get(subject_id)
        if state is None:
            state = _ControlState()
            self._states[subject_id] = state
        return state

    def evaluate(self, subject_id: str) -> MemoryTriggerDecision:
        state = self._state(subject_id)
        if state.previous_status in {"pending", "ingesting"}:
            return MemoryTriggerDecision(
                should_trigger=False,
                reason="previous_memory_process_not_finished",
            )
        if state.previous_status == "failed" and state.attempts >= self._max_retry:
            return MemoryTriggerDecision(
                should_trigger=False,
                reason="max_retry_reached",
            )

        batch = self._ledger.build_memory_batch(
            subject_id,
            min_chars=self._min_chars,
            min_segments=self._min_segments,
            max_age_seconds=self._max_age_seconds,
        )
        if batch is None:
            return MemoryTriggerDecision(
                should_trigger=False,
                reason="insufficient_memory_data",
            )
        return MemoryTriggerDecision(
            should_trigger=True,
            reason="memory_batch_ready",
            from_sequence=batch.from_sequence,
            to_sequence=batch.to_sequence,
        )

    def run_once(self, subject_id: str) -> MemoryControlResult:
        decision = self.evaluate(subject_id)
        if not decision.should_trigger:
            return MemoryControlResult(
                decision=decision,
                status="skipped",
            )

        batch = self._ledger.build_memory_batch(
            subject_id,
            min_chars=self._min_chars,
            min_segments=self._min_segments,
            max_age_seconds=self._max_age_seconds,
        )
        if batch is None:
            return MemoryControlResult(
                decision=decision,
                status="skipped",
            )

        entry = self._ledger.register_ingest(batch)
        self._ledger.mark_ingesting(entry.ingest_id)
        state = self._state(subject_id)
        state.previous_status = "ingesting"
        try:
            result = self._memory.ingest_batch(batch)
            consumed = result.consumed_through_sequence
            if consumed <= 0:
                raise RuntimeError("memory backend returned no consumed sequence")
            self._ledger.advance_consumer_cursor(
                subject_id,
                ConsumerKind.MEMORY,
                through_sequence=consumed,
            )
            self._ledger.mark_ingested(
                entry.ingest_id,
                memory_event_ids=result.memory_event_ids,
                stored_marks=result.stored_marks,
            )
            state.previous_status = "idle"
            state.attempts = 0
            state.last_error = ""
            return MemoryControlResult(
                decision=decision,
                status="ingested",
                ingest_id=entry.ingest_id,
            )
        except Exception as exc:  # 失败隔离：保留原文，允许重试
            state.previous_status = "failed"
            state.attempts += 1
            state.last_error = str(exc)
            self._ledger.mark_failed(entry.ingest_id, reason=str(exc))
            return MemoryControlResult(
                decision=decision,
                status="failed",
                ingest_id=entry.ingest_id,
                error=str(exc),
            )

    def monitor(self, subject_id: str) -> MemoryProcessStatus:
        state = self._state(subject_id)
        memory_start = self._ledger.consumer_cursor(subject_id, ConsumerKind.MEMORY)
        pending = self._ledger.list_experiences(
            subject_id, after_sequence=memory_start
        )
        return MemoryProcessStatus(
            subject_id=subject_id,
            previous_status=state.previous_status,
            attempts=state.attempts,
            last_error=state.last_error,
            pending_sequences=tuple(segment.sequence for segment in pending),
        )
