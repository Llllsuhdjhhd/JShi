"""100 价值库：独立表，不与 generic personal_items 混写。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from jshi.subject.domain import PersonalItem, PersonalKind, PersonalStatus, utc_now


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    data = json.loads(raw)
    return tuple(str(item) for item in data)


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw)


@dataclass(frozen=True)
class ValueLoadGate:
    catalog_revision: int = 0
    last_loaded_revision: int = -1
    last_loaded_at: datetime | None = None
    loaded_ids: tuple[str, ...] = ()


class ValueStore(Protocol):
    def get(self, entry_id: str) -> PersonalItem | None: ...

    def list(
        self,
        subject_id: str,
        *,
        role: str | None = None,
        status: str | None = None,
    ) -> tuple[PersonalItem, ...]: ...

    def put(self, item: PersonalItem) -> PersonalItem: ...

    def update_status(self, entry_id: str, status: PersonalStatus) -> PersonalItem: ...

    def update_metadata(
        self, entry_id: str, metadata: Mapping[str, Any]
    ) -> PersonalItem: ...

    def catalog_revision(self, subject_id: str) -> int: ...

    def bump_catalog(self, subject_id: str) -> int: ...

    def load_gate(self, subject_id: str) -> ValueLoadGate: ...

    def mark_loaded(
        self,
        subject_id: str,
        *,
        loaded_ids: Sequence[str],
        at: datetime | None = None,
    ) -> ValueLoadGate: ...


class SqliteValueStore:
    """与 subject.sqlite3 同库的 value_entries 表。"""

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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS value_entries (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    stance TEXT NOT NULL DEFAULT '',
                    source_type TEXT,
                    source_id TEXT,
                    source_ids TEXT NOT NULL,
                    domains TEXT NOT NULL,
                    context_tags TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 1.0,
                    status TEXT NOT NULL,
                    binding INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    use_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS value_catalog (
                    subject_id TEXT PRIMARY KEY,
                    catalog_revision INTEGER NOT NULL DEFAULT 0,
                    last_loaded_revision INTEGER NOT NULL DEFAULT -1,
                    last_loaded_at TEXT,
                    loaded_ids TEXT NOT NULL DEFAULT '[]'
                );
                """
            )

    def get(self, entry_id: str) -> PersonalItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM value_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        return _item_from_row(row) if row else None

    def list(
        self,
        subject_id: str,
        *,
        role: str | None = None,
        status: str | None = None,
    ) -> tuple[PersonalItem, ...]:
        sql = "SELECT * FROM value_entries WHERE subject_id = ?"
        params: list[Any] = [subject_id]
        if role:
            sql += " AND role = ?"
            params.append(role)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at, id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(_item_from_row(row) for row in rows)

    def put(self, item: PersonalItem) -> PersonalItem:
        meta = dict(item.metadata)
        role = str(meta.get("role", "value"))
        binding = meta.get("binding")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO value_entries (
                    id, subject_id, role, version, summary, content, stance,
                    source_type, source_id, source_ids, domains, context_tags,
                    importance, status, binding, created_at, updated_at,
                    last_used_at, use_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.subject_id,
                    role,
                    item.revision,
                    str(meta.get("summary", "")),
                    item.content,
                    str(meta.get("stance", "")),
                    meta.get("source_type"),
                    meta.get("source_id"),
                    _dump(list(item.source_ids)),
                    _dump(list(meta.get("domains", ()))),
                    _dump(list(meta.get("context_tags", ()))),
                    float(meta.get("importance", 1.0)),
                    item.status.value,
                    None if binding is None else (1 if binding else 0),
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                    meta.get("last_used_at"),
                    int(meta.get("use_count", 0)),
                ),
            )
        return item

    def update_status(self, entry_id: str, status: PersonalStatus) -> PersonalItem:
        current = self.get(entry_id)
        if current is None:
            raise KeyError(entry_id)
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE value_entries
                SET status = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (status.value, now, entry_id),
            )
        updated = self.get(entry_id)
        assert updated is not None
        return updated

    def update_metadata(
        self, entry_id: str, metadata: Mapping[str, Any]
    ) -> PersonalItem:
        current = self.get(entry_id)
        if current is None:
            raise KeyError(entry_id)
        merged = {**dict(current.metadata), **metadata}
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE value_entries
                SET summary = ?, stance = ?, importance = ?,
                    last_used_at = ?, use_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(merged.get("summary", "")),
                    str(merged.get("stance", "")),
                    float(merged.get("importance", 1.0)),
                    merged.get("last_used_at"),
                    int(merged.get("use_count", 0)),
                    now,
                    entry_id,
                ),
            )
        updated = self.get(entry_id)
        assert updated is not None
        return updated

    def catalog_revision(self, subject_id: str) -> int:
        return self.load_gate(subject_id).catalog_revision

    def bump_catalog(self, subject_id: str) -> int:
        gate = self.load_gate(subject_id)
        revision = gate.catalog_revision + 1
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO value_catalog (
                    subject_id, catalog_revision, last_loaded_revision,
                    last_loaded_at, loaded_ids
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    catalog_revision = excluded.catalog_revision
                """,
                (
                    subject_id,
                    revision,
                    gate.last_loaded_revision,
                    gate.last_loaded_at.isoformat() if gate.last_loaded_at else None,
                    _dump(list(gate.loaded_ids)),
                ),
            )
        return revision

    def load_gate(self, subject_id: str) -> ValueLoadGate:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM value_catalog WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
        if row is None:
            return ValueLoadGate()
        return ValueLoadGate(
            catalog_revision=int(row["catalog_revision"]),
            last_loaded_revision=int(row["last_loaded_revision"]),
            last_loaded_at=_parse_time(row["last_loaded_at"]),
            loaded_ids=_load_list(row["loaded_ids"]),
        )

    def mark_loaded(
        self,
        subject_id: str,
        *,
        loaded_ids: Sequence[str],
        at: datetime | None = None,
    ) -> ValueLoadGate:
        gate = self.load_gate(subject_id)
        stamp = (at or utc_now()).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO value_catalog (
                    subject_id, catalog_revision, last_loaded_revision,
                    last_loaded_at, loaded_ids
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    last_loaded_revision = excluded.last_loaded_revision,
                    last_loaded_at = excluded.last_loaded_at,
                    loaded_ids = excluded.loaded_ids
                """,
                (
                    subject_id,
                    gate.catalog_revision,
                    gate.catalog_revision,
                    stamp,
                    _dump(list(loaded_ids)),
                ),
            )
        return self.load_gate(subject_id)


def _item_from_row(row: sqlite3.Row) -> PersonalItem:
    binding = row["binding"]
    metadata: dict[str, Any] = {
        "role": row["role"],
        "source_type": row["source_type"],
        "summary": row["summary"] or "",
        "stance": row["stance"] or "",
        "domains": _load_list(row["domains"]),
        "context_tags": _load_list(row["context_tags"]),
        "importance": float(row["importance"] or 1.0),
        "use_count": int(row["use_count"] or 0),
    }
    if row["source_id"]:
        metadata["source_id"] = row["source_id"]
    if binding is not None:
        metadata["binding"] = bool(binding)
    if row["last_used_at"]:
        metadata["last_used_at"] = row["last_used_at"]
    return PersonalItem(
        id=row["id"],
        subject_id=row["subject_id"],
        kind=PersonalKind.VALUE,
        content=row["content"],
        source_ids=_load_list(row["source_ids"]),
        status=PersonalStatus(row["status"]),
        metadata=metadata,
        revision=int(row["version"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
