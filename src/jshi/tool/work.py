"""Persistent work/plan index. Stores relationships, never replays engine calls."""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping


class WorkIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._by_task: dict[str, set[str]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("work_id"):
                    self._records[row["work_id"]] = row
        for work_id, row in self._records.items():
            for task_id in row["tasks"].values():
                self._by_task.setdefault(task_id, set()).add(work_id)

    def _save(self, row: dict[str, Any]) -> None:
        old = self._records.get(row["work_id"], {})
        for task_id in old.get("tasks", {}).values():
            self._by_task.get(task_id, set()).discard(row["work_id"])
        self._records[row["work_id"]] = row
        for task_id in row["tasks"].values():
            self._by_task.setdefault(task_id, set()).add(row["work_id"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def create(self, work_id: str, subject_id: str, object_id: str, need: str,
               plan: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            if work_id in self._records:
                return
            self._save({"work_id": work_id, "subject_id": subject_id,
                        "object_id": object_id, "need": need, "plan": dict(plan or {}),
                        "tasks": {}, "handling": {}, "closed": False})

    def bind(self, work_id: str, task_id: str, step_id: str = "") -> None:
        with self._lock:
            row = deepcopy(self._records[work_id])
            row["tasks"][step_id or task_id] = task_id
            row["closed"] = False
            self._save(row)

    def get(self, work_id: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self._records.get(work_id))

    def for_object(self, subject_id: str, object_id: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(deepcopy(row) for row in self._records.values()
                         if row["subject_id"] == subject_id and row["object_id"] == object_id)

    def for_task(self, task_id: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(deepcopy(self._records[work_id])
                         for work_id in sorted(self._by_task.get(task_id, ())))

    def handle(self, work_id: str, task_id: str, handling: Mapping[str, Any]) -> None:
        with self._lock:
            row = deepcopy(self._records[work_id])
            row["handling"][task_id] = dict(handling)
            if handling.get("disposition") == "deferred":
                row["closed"] = False
            if row == self._records[work_id]:
                return
            self._save(row)

    def close(self, work_id: str, evidence: str, *, cancelled: bool = False) -> None:
        with self._lock:
            row = deepcopy(self._records[work_id])
            row["closed"] = True
            row["delivery_evidence"] = evidence
            row["cancelled"] = cancelled
            self._save(row)
