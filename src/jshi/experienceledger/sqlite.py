from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from jshi.core.params import active_zone_chars
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
        "style_pack_id": view.style_pack_id,
        "covered_event_ids": list(view.covered_event_ids),
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
        style_pack_id=str(data.get("style_pack_id") or ""),
        covered_event_ids=tuple(data.get("covered_event_ids") or ()),
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


def _meta_to_dict(state: _SubjectLedgerState) -> dict[str, Any]:
    return {
        "cursors": {kind.value: sequence for kind, sequence in state.cursors.items()},
        "context": _context_to_dict(state.context),
        "pending_sequences": list(state.pending_sequences),
    }


def _state_from_rows(
    data: dict[str, Any],
    segments: list[dict[str, Any]],
    ingest_entries: list[dict[str, Any]],
) -> _SubjectLedgerState:
    cursors = {
        ConsumerKind.ACTIVE_ZONE: 0,
        ConsumerKind.MEMORY: 0,
        ConsumerKind.AUDIT: 0,
    }
    for raw_kind, sequence in (data.get("cursors") or {}).items():
        cursors[ConsumerKind(raw_kind)] = int(sequence)
    return _SubjectLedgerState(
        segments=[_segment_from_dict(item) for item in segments],
        cursors=cursors,
        ingest_entries=[_ingest_from_dict(item) for item in ingest_entries],
        context=_context_from_dict(data.get("context")),
        pending_sequences=[int(item) for item in data.get("pending_sequences") or ()],
    )


class SqliteExperienceLedger(InProcessExperienceLedger):
    """与 subject.sqlite3 同库的经历账本。一段一行，写入只落改动的行；新实例读回上一份活跃区。

    experience_ledger 只存游标、现场和待处理序号；旧版整份 payload 在首次读取时迁移。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        active_window_chars: int | None = None,
    ) -> None:
        super().__init__(
            active_window_chars=(
                active_zone_chars()
                if active_window_chars is None
                else active_window_chars
            )
        )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._hydrated: set[str] = set()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            with connection:
                self._create_tables(connection)
        finally:
            connection.close()

    @staticmethod
    def _create_tables(connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS experience_ledger (
                subject_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS experience_segments (
                subject_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                segment_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (subject_id, sequence)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS experience_ingest (
                subject_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                ingest_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                PRIMARY KEY (subject_id, position)
            )
            """
        )

    def _load(self, subject_id: str) -> _SubjectLedgerState | None:
        connection = self._connect()
        try:
            with connection:
                return self._load_rows(connection, subject_id)
        finally:
            connection.close()

    def _load_rows(self, connection, subject_id: str) -> _SubjectLedgerState | None:
        row = connection.execute(
            "SELECT payload FROM experience_ledger WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload"])
        if "segments" in data or "ingest_entries" in data:
            state = _state_from_rows(
                data, data.get("segments") or [], data.get("ingest_entries") or []
            )
            # One transaction: the old payload is replaced only after every row is written.
            self._write(connection, subject_id, state,
                        segments=state.segments, ingest=state.ingest_entries)
            return state
        segments = [
            json.loads(item["payload"])
            for item in connection.execute(
                "SELECT payload FROM experience_segments WHERE subject_id = ? ORDER BY sequence",
                (subject_id,),
            )
        ]
        ingest = [
            json.loads(item["payload"])
            for item in connection.execute(
                "SELECT payload FROM experience_ingest WHERE subject_id = ? ORDER BY position",
                (subject_id,),
            )
        ]
        return _state_from_rows(data, segments, ingest)

    @staticmethod
    def _write(connection, subject_id, state, *, segments, ingest) -> None:
        connection.executemany(
            """
            INSERT INTO experience_segments(subject_id, sequence, segment_id, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subject_id, sequence) DO UPDATE SET
                segment_id = excluded.segment_id, payload = excluded.payload
            """,
            [
                (subject_id, segment.sequence, segment.segment_id, _dump(_segment_to_dict(segment)))
                for segment in segments
            ],
        )
        positions = {entry.ingest_id: index for index, entry in enumerate(state.ingest_entries)}
        connection.executemany(
            """
            INSERT INTO experience_ingest(subject_id, position, ingest_id, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subject_id, position) DO UPDATE SET
                ingest_id = excluded.ingest_id, payload = excluded.payload
            """,
            [
                (subject_id, positions[entry.ingest_id], entry.ingest_id, _dump(_ingest_to_dict(entry)))
                for entry in ingest
            ],
        )
        connection.execute(
            """
            INSERT INTO experience_ledger(subject_id, payload)
            VALUES (?, ?)
            ON CONFLICT(subject_id) DO UPDATE SET payload = excluded.payload
            """,
            (subject_id, _dump(_meta_to_dict(state))),
        )

    def _state(self, subject_id: str) -> _SubjectLedgerState:
        if subject_id not in self._hydrated:
            loaded = self._load(subject_id)
            if loaded is not None:
                self._states[subject_id] = loaded
            self._hydrated.add(subject_id)
        return super()._state(subject_id)

    def _after_write(self, subject_id: str) -> None:
        state = self._states[subject_id]
        segments = [s for s in state.segments if s.sequence in state.dirty_segments] \
            if state.dirty_segments else []
        ingest = [entry for entry in state.ingest_entries if entry.ingest_id in state.dirty_ingest] \
            if state.dirty_ingest else []
        connection = self._connect()
        try:
            with connection:
                self._write(connection, subject_id, state, segments=segments, ingest=ingest)
        finally:
            connection.close()
        state.dirty_segments.clear()
        state.dirty_ingest.clear()
