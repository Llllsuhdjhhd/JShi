from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_object_id() -> str:
    return f"OBJ-{uuid4().hex}"


@dataclass(frozen=True)
class ObjectProfile:
    """对话对象档案：身份本体 + 确认状态（不存单次置信度）。"""

    object_id: str
    label: str
    aliases: tuple[str, ...] = ()
    channel: str | None = None
    source: str = ""
    status: str = "provisional"  # unknown | provisional | confirmed | rejected
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


class ObjectProfileRepository:
    """SQLite 对象档案仓库（与 subject.sqlite3 同库），接口可替换。"""

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
                CREATE TABLE IF NOT EXISTS object_profiles (
                    object_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    aliases TEXT NOT NULL,
                    channel TEXT,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def create(self, profile: ObjectProfile) -> ObjectProfile:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO object_profiles
                (object_id, label, aliases, channel, source, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.object_id,
                    profile.label,
                    json.dumps(list(profile.aliases), ensure_ascii=False),
                    profile.channel,
                    profile.source,
                    profile.status,
                    profile.created_at.isoformat(),
                    profile.updated_at.isoformat(),
                ),
            )
        return profile

    def get(self, object_id: str) -> ObjectProfile | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM object_profiles WHERE object_id = ?", (object_id,)
            ).fetchone()
        return _profile(row) if row else None

    def find_by_name(self, name: str) -> ObjectProfile | None:
        """按 label 或别名精确匹配（忽略大小写）。"""
        key = name.strip().casefold()
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM object_profiles").fetchall()
        for row in rows:
            profile = _profile(row)
            if profile.label.casefold() == key:
                return profile
            if any(alias.casefold() == key for alias in profile.aliases):
                return profile
        return None

    def find_by_channel(self, channel: str) -> ObjectProfile | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM object_profiles WHERE channel = ?", (channel,)
            ).fetchone()
        return _profile(row) if row else None

    def update_status(self, object_id: str, status: str) -> ObjectProfile:
        current = self.get(object_id)
        if current is None:
            raise KeyError(object_id)
        updated = replace(current, status=status, updated_at=utc_now())
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE object_profiles
                SET status = ?, updated_at = ?
                WHERE object_id = ?
                """,
                (status, updated.updated_at.isoformat(), object_id),
            )
        return updated

    def list(self) -> Sequence[ObjectProfile]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM object_profiles ORDER BY created_at"
            ).fetchall()
        return tuple(_profile(row) for row in rows)


def _profile(row: sqlite3.Row) -> ObjectProfile:
    return ObjectProfile(
        object_id=row["object_id"],
        label=row["label"],
        aliases=tuple(json.loads(row["aliases"])),
        channel=row["channel"],
        source=row["source"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
