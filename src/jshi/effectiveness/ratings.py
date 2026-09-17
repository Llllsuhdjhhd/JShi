"""现场打分存储：一行一轮，不进 MemoryBackendPort。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jshi.models import MemoryRatings

from .port import utc_now


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_dt(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass
class RatingRow:
    subject_id: str
    activity_id: str
    coverage: str = ""
    gap_query: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    gap_consumed: bool = False
    analyzed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "activity_id": self.activity_id,
            "coverage": self.coverage,
            "gap_query": self.gap_query,
            "items": list(self.items),
            "created_at": _iso(self.created_at),
            "gap_consumed": self.gap_consumed,
            "analyzed": self.analyzed,
        }


def _items_from_ratings(ratings: MemoryRatings) -> list[dict[str, Any]]:
    return [
        {
            "ref": item.ref,
            "relevance": item.relevance,
            "helps_understanding": item.helps_understanding,
            "used_in_reply": item.used_in_reply,
            "misleading": item.misleading,
            "redundant": item.redundant,
            "object_fit": item.object_fit,
        }
        for item in ratings.items
    ]


def row_from_ratings(
    subject_id: str,
    activity_id: str,
    ratings: MemoryRatings,
    *,
    created_at: datetime | None = None,
) -> RatingRow:
    return RatingRow(
        subject_id=subject_id,
        activity_id=activity_id,
        coverage=ratings.coverage,
        gap_query=(ratings.gap_query or "").strip(),
        items=_items_from_ratings(ratings),
        created_at=created_at or utc_now(),
    )


class JsonlRatingStore:
    """`{data_dir}/memory_ratings.jsonl`；无路径则只留进程内。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._rows: list[RatingRow] = []
        if self._path is not None and self._path.exists():
            self._load()

    def append(self, row: RatingRow) -> RatingRow:
        self._rows.append(row)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row.to_json(), ensure_ascii=False) + "\n")
        return row

    def list(self, subject_id: str | None = None) -> tuple[RatingRow, ...]:
        rows = self._rows
        if subject_id is not None:
            rows = [row for row in rows if row.subject_id == subject_id]
        return tuple(rows)

    def pending_gap(self, subject_id: str) -> RatingRow | None:
        pending = [
            row
            for row in self._rows
            if row.subject_id == subject_id
            and (row.gap_query or "").strip()
            and not row.gap_consumed
        ]
        if not pending:
            return None
        return max(pending, key=lambda row: row.created_at)

    def consume_gap(self, subject_id: str) -> None:
        row = self.pending_gap(subject_id)
        if row is None:
            return
        row.gap_consumed = True
        self._rewrite()

    def mark_analyzed(self, rows: list[RatingRow]) -> None:
        ids = {(row.subject_id, row.activity_id, _iso(row.created_at)) for row in rows}
        for row in self._rows:
            key = (row.subject_id, row.activity_id, _iso(row.created_at))
            if key in ids:
                row.analyzed = True
        self._rewrite()

    def _load(self) -> None:
        assert self._path is not None
        loaded: list[RatingRow] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            payload = json.loads(text)
            if not isinstance(payload, dict):
                continue
            loaded.append(
                RatingRow(
                    subject_id=str(payload.get("subject_id") or ""),
                    activity_id=str(payload.get("activity_id") or ""),
                    coverage=str(payload.get("coverage") or ""),
                    gap_query=str(payload.get("gap_query") or ""),
                    items=list(payload.get("items") or []),
                    created_at=_parse_dt(str(payload.get("created_at") or "")),
                    gap_consumed=bool(payload.get("gap_consumed", False)),
                    analyzed=bool(payload.get("analyzed", False)),
                )
            )
        self._rows = loaded

    def _rewrite(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(
            json.dumps(row.to_json(), ensure_ascii=False) + "\n" for row in self._rows
        )
        self._path.write_text(body, encoding="utf-8")
