from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from jshi.core.params import ACTIVE_ZONE_CHARS
from jshi.experienceledger.inprocess import InProcessExperienceLedger, _SubjectLedgerState
from jshi.experienceledger.port import (
    ActorKind,
    ConsumerKind,
    ContextViewState,
    ExperienceSegment,
    MemoryIngestLedgerEntry,
    OutputKind,
    SegmentStatus,
    empty_context_view,
)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def _segment_to_dict(segment: ExperienceSegment) -> dict[str, Any]:
    return {
        "segment_id": segment.segment_id,
        "sequence": segment.sequence,
        "subject_id": segment.subject_id,
        "actor_kind": segment.actor_kind.value,
        "output_kind": segment.output_kind.value,
        "actor_object_id": segment.actor_object_id,
        "mentioned_object_ids": list(segment.mentioned_object_ids),
        "text_raw": segment.text_raw,
        "state_delta": dict(segment.state_delta) if segment.state_delta else None,
        "objects": dict(segment.objects) if segment.objects else None,
        "response_plan": dict(segment.response_plan) if segment.response_plan else None,
        "response_statuses": list(segment.response_statuses),
        "source_ids": list(segment.source_ids),
        "occurred_at": segment.occurred_at.isoformat(),
        "status": segment.status.value,
    }


def _segment_from_dict(data: dict[str, Any]) -> ExperienceSegment:
    occurred = _parse_time(data.get("occurred_at"))
    if occurred is None:
        raise ValueError("segment missing occurred_at")
    return ExperienceSegment(
        segment_id=str(data["segment_id"]),
        sequence=int(data["sequence"]),
        subject_id=str(data["subject_id"]),
        actor_kind=ActorKind(data["actor_kind"]),
        output_kind=OutputKind(data["output_kind"]),
        actor_object_id=data.get("actor_object_id"),
        mentioned_object_ids=tuple(data.get("mentioned_object_ids") or ()),
        text_raw=data.get("text_raw"),
        state_delta=data.get("state_delta"),
        objects=data.get("objects"),
        response_plan=data.get("response_plan"),
        response_statuses=tuple(data.get("response_statuses") or ()),
        source_ids=tuple(data.get("source_ids") or ()),
        occurred_at=occurred,
        status=SegmentStatus(data.get("status") or SegmentStatus.ACCEPTED.value),
    )


def _context_to_dict(view: ContextViewState) -> dict[str, Any]:
    return {
        "version": view.version,
        "context_text": view.context_text,
        "segment_refs": list(view.segment_refs),
        "segment_texts": [list(pair) for pair in view.segment_texts],
        "recall_excerpts": [list(pair) for pair in view.recall_excerpts],
        "speaker_object_id": view.speaker_object_id,
        "focused_refs": list(view.focused_refs),
        "last_applied_sequence": view.last_applied_sequence,
    }


def _context_from_dict(data: dict[str, Any] | None) -> ContextViewState:
    if not data:
        return empty_context_view()
    return ContextViewState(
        version=int(data.get("version") or 0),
        context_text=str(data.get("context_text") or ""),
        segment_refs=tuple(data.get("segment_refs") or ()),
        segment_texts=tuple(
            (str(ref), str(text)) for ref, text in (data.get("segment_texts") or ())
        ),
        recall_excerpts=tuple(
            (str(ref), str(text)) for ref, text in (data.get("recall_excerpts") or ())
        ),
        speaker_object_id=data.get("speaker_object_id"),
        focused_refs=tuple(data.get("focused_refs") or ()),
        last_applied_sequence=int(data.get("last_applied_sequence") or 0),
    )


def _ingest_to_dict(entry: MemoryIngestLedgerEntry) -> dict[str, Any]:
    return {
        "ingest_id": entry.ingest_id,
        "batch_id": entry.batch_id,
        "status": entry.status,
        "attempts": entry.attempts,
        "memory_event_ids": list(entry.memory_event_ids),
        "stored_marks": dict(entry.stored_marks),
        "reason": entry.reason,
        "ingested_at": entry.ingested_at.isoformat() if entry.ingested_at else None,
        "archived_at": entry.archived_at.isoformat() if entry.archived_at else None,
    }


def _ingest_from_dict(data: dict[str, Any]) -> MemoryIngestLedgerEntry:
    return MemoryIngestLedgerEntry(
        ingest_id=str(data["ingest_id"]),
        batch_id=str(data["batch_id"]),
        status=str(data.get("status") or "pending"),
        attempts=int(data.get("attempts") or 0),
        memory_event_ids=tuple(data.get("memory_event_ids") or ()),
        stored_marks=dict(data.get("stored_marks") or {}),
        reason=str(data.get("reason") or ""),
        ingested_at=_parse_time(data.get("ingested_at")),
        archived_at=_parse_time(data.get("archived_at")),
    )


def _state_to_dict(state: _SubjectLedgerState) -> dict[str, Any]:
    return {
        "segments": [_segment_to_dict(segment) for segment in state.segments],
        "cursors": {kind.value: sequence for kind, sequence in state.cursors.items()},
        "ingest_entries": [_ingest_to_dict(entry) for entry in state.ingest_entries],
        "context": _context_to_dict(state.context),
        "pending_sequences": list(state.pending_sequences),
    }


def _state_from_dict(data: dict[str, Any]) -> _SubjectLedgerState:
    cursors = {
        ConsumerKind.ACTIVE_ZONE: 0,
        ConsumerKind.MEMORY: 0,
        ConsumerKind.AUDIT: 0,
    }
    for raw_kind, sequence in (data.get("cursors") or {}).items():
        cursors[ConsumerKind(raw_kind)] = int(sequence)
    return _SubjectLedgerState(
        segments=[_segment_from_dict(item) for item in data.get("segments") or ()],
        cursors=cursors,
        ingest_entries=[
            _ingest_from_dict(item) for item in data.get("ingest_entries") or ()
        ],
        context=_context_from_dict(data.get("context")),
        pending_sequences=[int(item) for item in data.get("pending_sequences") or ()],
    )


class SqliteExperienceLedger(InProcessExperienceLedger):
    """与 subject.sqlite3 同库的经历账本。变更后落盘；新实例读回上一份活跃区。"""

    def __init__(
        self,
        path: str | Path,
        *,
        active_window_chars: int = ACTIVE_ZONE_CHARS,
    ) -> None:
        super().__init__(active_window_chars=active_window_chars)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._hydrated: set[str] = set()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS experience_ledger (
                    subject_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )

    def _load(self, subject_id: str) -> _SubjectLedgerState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM experience_ledger WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
        if row is None:
            return None
        return _state_from_dict(json.loads(row["payload"]))

    def _state(self, subject_id: str) -> _SubjectLedgerState:
        if subject_id not in self._hydrated:
            loaded = self._load(subject_id)
            if loaded is not None:
                self._states[subject_id] = loaded
            self._hydrated.add(subject_id)
        return super()._state(subject_id)

    def _after_write(self, subject_id: str) -> None:
        state = self._states[subject_id]
        payload = _dump(_state_to_dict(state))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO experience_ledger(subject_id, payload)
                VALUES (?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET payload = excluded.payload
                """,
                (subject_id, payload),
            )
