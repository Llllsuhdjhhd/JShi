"""调优建议。本阶段只追加，不改旧行，也不让建议生效。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from jshi.memory.strategy import RecallStrategyStore

CODE_REF = "src/jshi/effectiveness/memory_analyzer.py"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


@dataclass
class Suggestion:
    status: str
    created_at: datetime = field(default_factory=_now)
    id: str = ""
    subject_id: str = ""
    counter_id: str = ""
    window_start: str = ""
    window_end: str = ""
    count: float | None = None
    sample_activity_ids: tuple[str, ...] = ()
    cause: str = ""
    code_ref: str = ""
    proposed: dict[str, Any] | None = None
    companion: dict[str, Any] | None = None
    constraint: str = ""
    applied: bool = False
    reject_reason: str = ""
    subjects: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid4())

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "created_at": _iso(self.created_at),
            "status": self.status,
            "applied": self.applied,
            "subject_id": self.subject_id,
            "counter_id": self.counter_id,
            "window": {"start": self.window_start, "end": self.window_end},
            "count": self.count,
            "sample_activity_ids": list(self.sample_activity_ids),
            "cause": self.cause,
            "code_ref": self.code_ref,
            "proposed": self.proposed,
            "companion": self.companion,
            "constraint": self.constraint,
            "reject_reason": self.reject_reason,
        }
        if self.subjects is not None:
            payload["subjects"] = self.subjects
        return payload


class SuggestionStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.rows: list[dict[str, Any]] = []
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    self.rows.append(item)

    def has_baseline(self) -> bool:
        return any(row.get("status") == "baseline" for row in self.rows)

    def append(self, suggestion: Suggestion) -> dict[str, Any]:
        payload = suggestion.to_json()
        self.rows.append(payload)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return payload


def write_baseline_if_absent(
    path: Path | str,
    strategy: RecallStrategyStore,
    subject_ids: tuple[str, ...] = (),
) -> bool:
    """停用自动改档时记下各主体当时的档位和条数。已有起点则不再写。"""
    store = SuggestionStore(path)
    if store.has_baseline():
        return False
    known = set(subject_ids) | set(strategy.known_ids())
    subjects: dict[str, Any] = {}
    for subject_id in sorted(known):
        current = strategy.get(subject_id)
        subjects[subject_id] = {
            "default_level": current.default_level,
            "limit": current.limit,
        }
    store.append(
        Suggestion(
            status="baseline",
            subjects=subjects,
            cause="停用自动改档时的档位和条数",
        )
    )
    return True
