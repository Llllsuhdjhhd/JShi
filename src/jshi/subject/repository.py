from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .domain import (
    Activity,
    ActivityKind,
    ActivityStatus,
    CognitiveContent,
    CognitiveKind,
    EpistemicStatus,
    EvidenceKind,
    HistoryKind,
    HistoryRecord,
    PersonalItem,
    PersonalKind,
    PersonalStatus,
    StateTransition,
    utc_now,
)

logger = logging.getLogger(__name__)


class SubjectRepository:
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
                CREATE TABLE IF NOT EXISTS personal_items (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_ids TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT '中',
                    entry_type TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS ix_personal_subject
                    ON personal_items(subject_id, kind, status);

                CREATE TABLE IF NOT EXISTS activities (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    intention_ids TEXT NOT NULL,
                    response_statuses TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_activity_subject
                    ON activities(subject_id, status);

                CREATE TABLE IF NOT EXISTS cognitive_contents (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    activity_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    epistemic_status TEXT NOT NULL,
                    evidence_kind TEXT NOT NULL,
                    source_ids TEXT NOT NULL,
                    model TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_cognitive_activity
                    ON cognitive_contents(activity_id, created_at);

                CREATE TABLE IF NOT EXISTS transitions (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS histories (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    subject_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_history_subject
                    ON histories(subject_id, kind, sequence);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(activities)"
                ).fetchall()
            }
            if "response_statuses" not in columns:
                connection.execute(
                    "ALTER TABLE activities ADD COLUMN response_statuses TEXT NOT NULL DEFAULT '[]'"
                )
            personal_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(personal_items)"
                ).fetchall()
            }
            if "level" not in personal_columns:
                connection.execute(
                    "ALTER TABLE personal_items ADD COLUMN level TEXT NOT NULL DEFAULT '中'"
                )
            if "entry_type" not in personal_columns:
                connection.execute(
                    "ALTER TABLE personal_items ADD COLUMN entry_type TEXT NOT NULL DEFAULT ''"
                )

    def add_personal_item(self, item: PersonalItem) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO personal_items (
                    id, subject_id, kind, content, source_ids, status, metadata,
                    revision, created_at, updated_at, level, entry_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.subject_id,
                    item.kind.value,
                    item.content,
                    _dump_ids(item.source_ids),
                    item.status.value,
                    json.dumps(item.metadata, ensure_ascii=False),
                    item.revision,
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                    item.level,
                    item.entry_type,
                ),
            )

    def get_personal_item(self, item_id: str) -> PersonalItem:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM personal_items WHERE id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return _personal(row)

    def list_personal_items(
        self,
        subject_id: str,
        kind: PersonalKind | None = None,
        active_only: bool = True,
    ) -> Sequence[PersonalItem]:
        clauses = ["subject_id = ?"]
        arguments: list[object] = [subject_id]
        if kind:
            clauses.append("kind = ?")
            arguments.append(kind.value)
        if active_only:
            clauses.append("status = ?")
            arguments.append(PersonalStatus.ACTIVE.value)
        query = "SELECT * FROM personal_items WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at"
        with self._connect() as connection:
            rows = connection.execute(query, arguments).fetchall()
        items: list[PersonalItem] = []
        for row in rows:
            try:
                items.append(_personal(row))
            except ValueError as exc:
                # 已废除种类（如旧数据里的 concern）不进入个人世界装载。
                logger.warning("skip legacy personal item: %s", exc)
        return tuple(items)

    def update_personal_status(
        self, item_id: str, status: PersonalStatus
    ) -> PersonalItem:
        item = self.get_personal_item(item_id)
        updated = replace(
            item,
            status=status,
            revision=item.revision + 1,
            updated_at=utc_now(),
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE personal_items
                SET status = ?, revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.status.value,
                    updated.revision,
                    updated.updated_at.isoformat(),
                    item_id,
                ),
            )
        return updated

    def update_personal_metadata(
        self, item_id: str, metadata: Mapping[str, Any]
    ) -> PersonalItem:
        """合并更新个人条目 metadata；值为 None 的键表示删除。

        活跃区剔除/恢复使用该通道（zone_state / evicted_at），
        不改变事件本体内容与 status。
        """
        current = self.get_personal_item(item_id)
        merged = dict(current.metadata)
        merged.update(metadata)
        cleaned = {key: value for key, value in merged.items() if value is not None}
        updated = replace(
            current,
            metadata=cleaned,
            revision=current.revision + 1,
            updated_at=utc_now(),
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE personal_items
                SET metadata = ?, revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(cleaned, ensure_ascii=False),
                    updated.revision,
                    updated.updated_at.isoformat(),
                    item_id,
                ),
            )
        return updated

    def add_activity(self, activity: Activity) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO activities (
                    id, subject_id, kind, trigger, status,
                    intention_ids, response_statuses, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    activity.id,
                    activity.subject_id,
                    activity.kind.value,
                    activity.trigger,
                    activity.status.value,
                    _dump_ids(activity.intention_ids),
                    _dump_ids(activity.response_statuses),
                    activity.created_at.isoformat(),
                    activity.updated_at.isoformat(),
                ),
            )

    def get_activity(self, activity_id: str) -> Activity:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM activities WHERE id = ?", (activity_id,)
            ).fetchone()
        if row is None:
            raise KeyError(activity_id)
        return _activity(row)

    def update_activity(
        self,
        activity_id: str,
        *,
        status: ActivityStatus | None = None,
        intention_ids: tuple[str, ...] | None = None,
        response_statuses: tuple[str, ...] | None = None,
        reason: str = "",
    ) -> Activity:
        current = self.get_activity(activity_id)
        updated = replace(
            current,
            status=status or current.status,
            intention_ids=(
                intention_ids if intention_ids is not None else current.intention_ids
            ),
            response_statuses=(
                response_statuses
                if response_statuses is not None
                else current.response_statuses
            ),
            updated_at=utc_now(),
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE activities
                SET status = ?, intention_ids = ?, response_statuses = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.status.value,
                    _dump_ids(updated.intention_ids),
                    _dump_ids(updated.response_statuses),
                    updated.updated_at.isoformat(),
                    activity_id,
                ),
            )
        if updated.status != current.status:
            transition = StateTransition(
                subject_id=updated.subject_id,
                target_type="activity",
                target_id=activity_id,
                from_state=current.status.value,
                to_state=updated.status.value,
                reason=reason,
            )
            self.add_transition(transition)
        return updated

    def list_activities(
        self, subject_id: str, status: ActivityStatus | None = None
    ) -> Sequence[Activity]:
        query = "SELECT * FROM activities WHERE subject_id = ?"
        arguments: list[object] = [subject_id]
        if status:
            query += " AND status = ?"
            arguments.append(status.value)
        query += " ORDER BY created_at"
        with self._connect() as connection:
            rows = connection.execute(query, arguments).fetchall()
        return tuple(_activity(row) for row in rows)

    def add_cognitive_content(self, content: CognitiveContent) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cognitive_contents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content.id,
                    content.subject_id,
                    content.activity_id,
                    content.kind.value,
                    content.content,
                    content.epistemic_status.value,
                    content.evidence_kind.value,
                    _dump_ids(content.source_ids),
                    content.model,
                    content.created_at.isoformat(),
                ),
            )

    def get_cognitive_content(self, content_id: str) -> CognitiveContent:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cognitive_contents WHERE id = ?", (content_id,)
            ).fetchone()
        if row is None:
            raise KeyError(content_id)
        return _cognitive(row)

    def update_epistemic_status(
        self, content_id: str, status: EpistemicStatus
    ) -> CognitiveContent:
        current = self.get_cognitive_content(content_id)
        updated = replace(current, epistemic_status=status)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE cognitive_contents
                SET epistemic_status = ?
                WHERE id = ?
                """,
                (status.value, content_id),
            )
        return updated

    def list_cognitive_contents(self, activity_id: str) -> Sequence[CognitiveContent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM cognitive_contents
                WHERE activity_id = ?
                ORDER BY created_at
                """,
                (activity_id,),
            ).fetchall()
        return tuple(_cognitive(row) for row in rows)

    def add_transition(self, transition: StateTransition) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO transitions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    transition.id,
                    transition.subject_id,
                    transition.target_type,
                    transition.target_id,
                    transition.from_state,
                    transition.to_state,
                    transition.reason,
                    _dump_ids(transition.source_ids),
                    transition.created_at.isoformat(),
                ),
            )

    def list_transitions(self, target_id: str) -> Sequence[StateTransition]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM transitions
                WHERE target_id = ?
                ORDER BY created_at
                """,
                (target_id,),
            ).fetchall()
        return tuple(_transition(row) for row in rows)

    def add_history(self, record: HistoryRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO histories
                (id, subject_id, kind, event_type, content, source_ids, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.subject_id,
                    record.kind.value,
                    record.event_type,
                    json.dumps(record.content, ensure_ascii=False),
                    _dump_ids(record.source_ids),
                    record.created_at.isoformat(),
                ),
            )

    def list_history(
        self, subject_id: str, kind: HistoryKind | None = None, limit: int = 100
    ) -> Sequence[HistoryRecord]:
        query = "SELECT * FROM histories WHERE subject_id = ?"
        arguments: list[object] = [subject_id]
        if kind:
            query += " AND kind = ?"
            arguments.append(kind.value)
        query += " ORDER BY sequence DESC LIMIT ?"
        arguments.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, arguments).fetchall()
        return tuple(reversed([_history(row) for row in rows]))


def _dump_ids(values: tuple[str, ...]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _load_ids(value: str) -> tuple[str, ...]:
    return tuple(json.loads(value))


def _personal(row: sqlite3.Row) -> PersonalItem:
    keys = set(row.keys())
    return PersonalItem(
        id=row["id"],
        subject_id=row["subject_id"],
        kind=PersonalKind(row["kind"]),
        content=row["content"],
        source_ids=_load_ids(row["source_ids"]),
        status=PersonalStatus(row["status"]),
        metadata=json.loads(row["metadata"]),
        level=row["level"] if "level" in keys else "中",
        entry_type=row["entry_type"] if "entry_type" in keys else "",
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _activity(row: sqlite3.Row) -> Activity:
    raw_status = row["status"]
    if raw_status == "waiting":
        raw_status = "completed"
    return Activity(
        id=row["id"],
        subject_id=row["subject_id"],
        kind=ActivityKind(row["kind"]),
        trigger=row["trigger"],
        status=ActivityStatus(raw_status),
        intention_ids=_load_ids(row["intention_ids"]),
        response_statuses=_load_ids(row["response_statuses"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _cognitive(row: sqlite3.Row) -> CognitiveContent:
    return CognitiveContent(
        id=row["id"],
        subject_id=row["subject_id"],
        activity_id=row["activity_id"],
        kind=CognitiveKind(row["kind"]),
        content=row["content"],
        epistemic_status=EpistemicStatus(row["epistemic_status"]),
        evidence_kind=EvidenceKind(row["evidence_kind"]),
        source_ids=_load_ids(row["source_ids"]),
        model=row["model"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _transition(row: sqlite3.Row) -> StateTransition:
    return StateTransition(
        id=row["id"],
        subject_id=row["subject_id"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        from_state=row["from_state"],
        to_state=row["to_state"],
        reason=row["reason"],
        source_ids=_load_ids(row["source_ids"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _history(row: sqlite3.Row) -> HistoryRecord:
    return HistoryRecord(
        id=row["id"],
        subject_id=row["subject_id"],
        kind=HistoryKind(row["kind"]),
        event_type=row["event_type"],
        content=json.loads(row["content"]),
        source_ids=_load_ids(row["source_ids"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
