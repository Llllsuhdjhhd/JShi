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
                CREATE TABLE IF NOT EXISTS object_terms (
                    object_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    source_input_id TEXT NOT NULL,
                    PRIMARY KEY (object_id, kind, text)
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

    def rename(self, object_id: str, name: str, *, expected_label: str | None = None, keep_alias: bool = False) -> ObjectProfile:
        """Change a label in place; references, voiceprints and memories keep their id."""
        name = name.strip()
        if not name or len(name) > 40 or any(ord(c) < 32 for c in name):
            raise ValueError('姓名须为 1–40 个可显示字符')
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM object_profiles WHERE object_id = ?', (object_id,)).fetchone()
            if row is None:
                raise KeyError(object_id)
            current = _profile(row)
            if expected_label is not None and current.label != expected_label:
                raise ValueError('姓名已变更，请刷新后再修改')
            aliases = current.aliases
            if keep_alias and current.label not in aliases:
                aliases = (*aliases, current.label)
            updated = replace(current, label=name, aliases=aliases, updated_at=utc_now())
            connection.execute('UPDATE object_profiles SET label = ?, aliases = ?, updated_at = ? WHERE object_id = ?',
                (name, json.dumps(list(aliases), ensure_ascii=False), updated.updated_at.isoformat(), object_id))
        return updated

    def add_carrier(self, object_id: str, carrier: CarrierEntry) -> ObjectProfile:
        # One transaction prevents associating a voice reference with two people.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT * FROM object_profiles").fetchall()
            profiles = tuple(_profile(row) for row in rows)
            current = next((p for p in profiles if p.object_id == object_id), None)
            if current is None:
                raise KeyError(object_id)
            for p in profiles:
                if p.object_id != object_id and any(c.kind == carrier.kind and c.value == carrier.value for c in p.carriers):
                    raise ValueError("carrier already belongs to another object")
            carriers = tuple(c for c in current.carriers if not (c.kind == carrier.kind and c.value == carrier.value)) + (carrier,)
            updated = replace(current, carriers=carriers, updated_at=utc_now())
            connection.execute("UPDATE object_profiles SET carriers = ?, updated_at = ? WHERE object_id = ?",
                               (_dump_carriers(carriers), updated.updated_at.isoformat(), object_id))
        return updated

    def add_alias(self, object_id: str, name: str) -> ObjectProfile:
        """Add an explicit name to this person; a name collision never merges ids."""
        name = name.strip()
        if not name or len(name) > 40 or any(ord(c) < 32 for c in name):
            raise ValueError("别名须为1–40个可显示字符")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM object_profiles WHERE object_id = ?", (object_id,)).fetchone()
            if row is None:
                raise KeyError(object_id)
            current = _profile(row)
            if name == current.label or name in current.aliases:
                return current
            updated = replace(current, aliases=(*current.aliases, name), updated_at=utc_now())
            connection.execute("UPDATE object_profiles SET aliases = ?, updated_at = ? WHERE object_id = ?",
                               (json.dumps(list(updated.aliases), ensure_ascii=False), updated.updated_at.isoformat(), object_id))
        return updated

    def note_term(self, object_id: str, kind: str, text: str, source_input_id: str = "") -> None:
        """记下一个人怎么称呼匠石，或别人怎么称呼他。原文必须已经出现在发言里。"""
        text = (text or "").strip()
        if not object_id or not text or kind not in {"calls_subject", "called_by_others"}:
            return
        now = utc_now().isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT count FROM object_terms WHERE object_id = ? AND kind = ? AND text = ?",
                (object_id, kind, text),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO object_terms
                    (object_id, kind, text, count, first_seen, last_seen, source_input_id)
                    VALUES (?, ?, ?, 1, ?, ?, ?)
                    """,
                    (object_id, kind, text, now, now, source_input_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE object_terms
                    SET count = ?, last_seen = ?, source_input_id = ?
                    WHERE object_id = ? AND kind = ? AND text = ?
                    """,
                    (int(row["count"]) + 1, now, source_input_id, object_id, kind, text),
                )

    def terms_of(self, object_id: str, kind: str, limit: int = 5) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT text FROM object_terms
                WHERE object_id = ? AND kind = ?
                ORDER BY count DESC, last_seen DESC LIMIT ?
                """,
                (object_id, kind, limit),
            ).fetchall()
        return tuple(row["text"] for row in rows)

    def all_terms(self, kind: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT text FROM object_terms WHERE kind = ? ORDER BY text",
                (kind,),
            ).fetchall()
        return tuple(row["text"] for row in rows)

    def address_line(self, object_id: str) -> str:
        """人物肖像里的一行：这个人还有哪些名字，以及他怎么称呼匠石。"""
        profile = self.get(object_id)
        if profile is None:
            return ""
        parts: list[str] = [f"人物：{profile.label}"]
        if profile.aliases:
            parts.append(f"{profile.label}，又名{'、'.join(profile.aliases)}")
        terms = self.terms_of(object_id, "calls_subject")
        if terms:
            parts.append("常称匠石为" + "、".join(f"「{item}」" for item in terms))
        other_terms = self.terms_of(object_id, "called_by_others")
        if other_terms:
            parts.append("别人称此人为" + "、".join(f"「{item}」" for item in other_terms))
        return "称呼：" + "；".join(parts) if parts else ""

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
