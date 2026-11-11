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
            self._save(row)

    def expire(self, work_id):
        with self._lock:
            row = deepcopy(self._records[work_id])
            row['expired'] = True
            self._save(row)

    def set_terms(self, work_id, *, waiting_for=(), expires_at=None, need=''):
        from .validity import deadline, pending_information
        # None preserves an existing deadline; empty string explicitly clears it.
        limit = deadline(expires_at) if expires_at is not None else None
        with self._lock:
            row = deepcopy(self._records[work_id])
            row['waiting_for'] = list(pending_information(waiting_for))
            if expires_at is not None:
                row['expires_at'] = limit.isoformat() if limit else ''
            if need:
                row['current_need'] = need
            self._save(row)

    def set_plan(self, work_id, plan):
        with self._lock:
            row = deepcopy(self._records[work_id])
            row['plan'] = dict(plan)
            self._save(row)

    def _project(self, row):
        value=deepcopy(row)
        owner=getattr(self,'person_owner',None)
        if value is not None and owner is not None: value['object_id']=owner(value)
        return value

    def get(self, work_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._project(self._records.get(work_id))

    def for_object(self, subject_id: str, object_id: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(self._project(row) for row in self._records.values()
                         if row['subject_id']==subject_id and self._project(row)['object_id']==object_id)

    def for_task(self, task_id: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(self._project(self._records[work_id])
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

    def notice(self, work_id: str, task_id: str, kind: str) -> None:
        """One durable terminal notice per task; reading/playing never creates one."""
        with self._lock:
            row = deepcopy(self._records[work_id])
            if row['closed'] or task_id in row.get('notices', {}):
                return
            row.setdefault('notices', {})[task_id] = {'kind': kind, 'handled': False}
            self._save(row)

    def acknowledge_notices(self, task_ids) -> None:
        with self._lock:
            for work_id, value in tuple(self._records.items()):
                row = deepcopy(value)
                changed = False
                for task_id in task_ids:
                    notice = row.get('notices', {}).get(task_id)
                    if notice and not notice['handled']:
                        notice['handled'] = True
                        changed = True
                if changed:
                    self._save(row)

    def attempt_notices(self, task_ids):
        """One automatic cognition attempt per terminal fact, durable across restart.

        Failure leaves the work open for the next human turn; polling must not
        turn a provider outage into an unlimited stream of paid retries.
        """
        wanted = set(task_ids)
        with self._lock:
            for row in self._records.values():
                changed = False
                for tid, notice in row.get('notices', {}).items():
                    if tid in wanted and not notice.get('attempted'):
                        notice['attempted'] = True
                        changed = True
                if changed:
                    self._save(row)

    def pending_notices(self, subject_id: str, object_id: str | None = None):
        with self._lock:
            return tuple({'work_id': row['work_id'], 'object_id': row['object_id'],
                          'task_id': tid, **notice}
                         for value in self._records.values() if not value['closed']
                         for row in (self._project(value),)
                         if row['subject_id'] == subject_id and (object_id is None or row['object_id'] == object_id)
                         for tid, notice in row.get('notices', {}).items() if not notice['handled'])
