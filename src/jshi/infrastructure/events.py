from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Sequence

from jshi.core import Event, EventKind, Provenance, TruthStatus


class EventStore(ABC):
    @abstractmethod
    def append(self, event: Event) -> None: ...

    @abstractmethod
    def get(self, event_id: str) -> Event | None: ...

    @abstractmethod
    def list_for_subject(self, subject_id: str, limit: int = 100) -> Sequence[Event]: ...


class SQLiteEventStore(EventStore):
    """Append-only local event store; derived interpretations remain separate events."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    subject_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    truth_status TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_events_subject "
                "ON events(subject_id, sequence)"
            )

    def append(self, event: Event) -> None:
        provenance = {
            "source": event.provenance.source,
            "source_event_ids": list(event.provenance.source_event_ids),
            "method": event.provenance.method,
            "model": event.provenance.model,
            "created_at": event.provenance.created_at.isoformat(),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events
                (id, subject_id, kind, truth_status, content, provenance,
                 created_at, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.subject_id,
                    event.kind.value,
                    event.truth_status.value,
                    json.dumps(event.content, ensure_ascii=False),
                    json.dumps(provenance, ensure_ascii=False),
                    event.created_at.isoformat(),
                    event.schema_version,
                ),
            )

    def get(self, event_id: str) -> Event | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return self._deserialize(row) if row else None

    def list_for_subject(self, subject_id: str, limit: int = 100) -> Sequence[Event]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE subject_id = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (subject_id, limit),
            ).fetchall()
        return tuple(reversed([self._deserialize(row) for row in rows]))

    @staticmethod
    def _deserialize(row: sqlite3.Row) -> Event:
        raw_provenance = json.loads(row["provenance"])
        provenance = Provenance(
            source=raw_provenance["source"],
            source_event_ids=tuple(raw_provenance["source_event_ids"]),
            method=raw_provenance["method"],
            model=raw_provenance["model"],
            created_at=datetime.fromisoformat(raw_provenance["created_at"]),
        )
        return Event(
            id=row["id"],
            subject_id=row["subject_id"],
            kind=EventKind(row["kind"]),
            truth_status=TruthStatus(row["truth_status"]),
            content=json.loads(row["content"]),
            provenance=provenance,
            created_at=datetime.fromisoformat(row["created_at"]),
            schema_version=row["schema_version"],
        )
