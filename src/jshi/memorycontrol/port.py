from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from jshi.experienceledger import ExperienceLedgerPort
from jshi.memory.contracts import BackendIngestResult, MemoryBatch


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MemoryTriggerDecision:
    should_trigger: bool
    reason: str
    from_sequence: int = 0
    to_sequence: int = 0


@dataclass(frozen=True)
class MemoryProcessStatus:
    subject_id: str
    previous_status: str
    attempts: int
    last_error: str = ""
    pending_sequences: tuple[int, ...] = ()


@dataclass(frozen=True)
class MemoryControlResult:
    decision: MemoryTriggerDecision
    status: str
    ingest_id: str = ""
    error: str = ""


class MemoryBatchIngestPort(Protocol):
    """09 薄壳：接收 MemoryBatch，返回后端接管结果。"""

    def ingest_batch(self, batch: MemoryBatch) -> BackendIngestResult:
        """把批次交给后端；返回 stored_marks / sealed_event_ids 等结果。"""


class MemoryControlPort(Protocol):
    def evaluate(self, subject_id: str) -> MemoryTriggerDecision: ...

    def run_once(self, subject_id: str) -> MemoryControlResult: ...

    def monitor(self, subject_id: str) -> MemoryProcessStatus: ...
