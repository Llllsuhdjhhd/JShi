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
class CarrierEntry:
    """识别载体条目（占位）：只存引用或标识，不存原始生物数据。"""

    kind: str  # voiceprint | face | device | account | session
    value: str  # 特征引用 / 设备标识 / 账号 id
    source: str = ""


@dataclass(frozen=True)
class ObjectProfile:
    """对话对象档案：身份本体 + 确认状态（不存单次置信度）。"""

    object_id: str
    label: str
    aliases: tuple[str, ...] = ()
    carriers: tuple[CarrierEntry, ...] = ()
    channel: str | None = None
    source: str = ""
    status: str = "provisional"  # provisional | confirmed | rejected
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
                    carriers TEXT NOT NULL,
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
                (object_id, label, aliases, carriers, channel, source, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.object_id,
                    profile.label,
                    json.dumps(list(profile.aliases), ensure_ascii=False),
                    _dump_carriers(profile.carriers),
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

    def find_by_names(self, name: str) -> tuple[ObjectProfile, ...]:
        """按 label 或别名精确匹配（忽略大小写），返回候选集（重名合法）。"""
        key = name.strip().casefold()
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM object_profiles").fetchall()
        hits: list[ObjectProfile] = []
        for row in rows:
            profile = _profile(row)
            if profile.label.casefold() == key:
                hits.append(profile)
                continue
            if any(alias.casefold() == key for alias in profile.aliases):
                hits.append(profile)
        return tuple(hits)

    def find_by_carrier(self, kind: str, value: str) -> ObjectProfile | None:
        """按识别载体引用匹配；载体引用唯一，多个匹配视为数据错误。"""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM object_profiles").fetchall()
        hits = [
            _profile(row)
            for row in rows
            if any(
                carrier.kind == kind and carrier.value == value
                for carrier in _profile(row).carriers
            )
        ]
        if len(hits) > 1:
            raise ValueError(
                f"carrier collision: {kind}:{value} maps to multiple objects"
            )
        return hits[0] if hits else None

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
        carriers=_load_carriers(row["carriers"]),
        channel=row["channel"],
        source=row["source"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _dump_carriers(carriers: Sequence[CarrierEntry]) -> str:
    return json.dumps(
        [
            {"kind": carrier.kind, "value": carrier.value, "source": carrier.source}
            for carrier in carriers
        ],
        ensure_ascii=False,
    )


def _load_carriers(raw: str) -> tuple[CarrierEntry, ...]:
    return tuple(
        CarrierEntry(
            kind=item["kind"],
            value=item["value"],
            source=item.get("source", ""),
        )
        for item in json.loads(raw)
    )
