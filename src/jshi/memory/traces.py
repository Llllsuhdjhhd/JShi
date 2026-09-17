"""召回触发流水：哪次输入召回了哪些记忆。不进 09，不叫记忆。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


@dataclass(frozen=True)
class RecallTrace:
    subject_id: str
    activity_id: str
    query: str
    object_id: str = ""
    input_segment_id: str = ""
    level: int = 1
    recalled_event_ids: tuple[str, ...] = ()
    skipped_ids: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=_utc_now)

    def to_json(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "activity_id": self.activity_id,
            "query": self.query,
            "object_id": self.object_id,
            "input_segment_id": self.input_segment_id,
            "level": self.level,
            "recalled_event_ids": list(self.recalled_event_ids),
            "skipped_ids": list(self.skipped_ids),
            "created_at": _iso(self.created_at),
        }


class JsonlRecallTraceStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._rows: list[RecallTrace] = []

    def append(self, trace: RecallTrace) -> None:
        self._rows.append(trace)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(trace.to_json(), ensure_ascii=False) + "\n")

    def list(self, subject_id: str | None = None) -> tuple[RecallTrace, ...]:
        if subject_id is None:
            return tuple(self._rows)
        return tuple(row for row in self._rows if row.subject_id == subject_id)
