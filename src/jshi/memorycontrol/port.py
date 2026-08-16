from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Protocol, Sequence

from jshi.activityledger import ActivityLedgerPort, MemoryBatch


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MemoryTriggerDecision:
    should_trigger: bool
    reason: str
    from_sequence: int = 0
    to_sequence: int = 0


@dataclass(frozen=True)
class MemoryIngestResult:
    consumed_through_sequence: int
    memory_event_ids: tuple[str, ...] = ()
    stored_marks: Mapping[str, str] = field(default_factory=dict)


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
    """09 薄壳待实现：接收 MemoryBatch，返回接管结果。"""

    def ingest_batch(self, batch: MemoryBatch) -> MemoryIngestResult:
        """把批次交给后端；确认接管后返回 consumed_through_sequence。"""


class MemoryControlPort(Protocol):
    def evaluate(self, subject_id: str) -> MemoryTriggerDecision: ...

    def run_once(self, subject_id: str) -> MemoryControlResult: ...

    def monitor(self, subject_id: str) -> MemoryProcessStatus: ...
