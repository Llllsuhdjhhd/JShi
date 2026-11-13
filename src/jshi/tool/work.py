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

    def define_goal(self, work_id, *, original='', requirements=()):
        with self._lock:
            row = deepcopy(self._records[work_id])
            if original and not row.get('original_request'):
                row['original_request'] = original
            if requirements and not row.get('requirements'):
                row['requirements'] = list(requirements)
            row.setdefault('revision', 0)
            if row != self._records[work_id]:
                self._save(row)

    def clarify(self, work_id, questions, source_id):
        with self._lock:
            row = deepcopy(self._records[work_id])
            entries = row.setdefault('clarifications', [])
            if not any(e['source_id'] == source_id for e in entries):
                entries.append({'source_id': source_id, 'questions': list(questions)})
                row['waiting_for'] = list(questions)
                self._save(row)

    def set_recording_goal(self, work_id, *, continuous, params=None):
        """Keep the accepted recording lifetime separate from one execution result."""
        with self._lock:
            row = deepcopy(self._records[work_id])
            goal = row.setdefault('recording_goal', {})
            goal['continuous'] = bool(continuous)
            if params is not None:
                goal['params'] = dict(params)
            if row != self._records[work_id]:
                self._save(row)

    def amend(self, work_id, text, source_id):
        with self._lock:
            row = deepcopy(self._records[work_id])
            entries = row.setdefault('amendments', [])
            if not any(e['source_id'] == source_id for e in entries):
                entries.append({'source_id': source_id, 'text': text})
                row['revision'] = row.get('revision', 0) + 1
                self._save(row)
            return row['revision']

    def review(self, work_id, request_id, result):
        with self._lock:
            row = deepcopy(self._records[work_id])
            reviews = row.setdefault('reviews', [])
            reviews.append({'request_id': request_id, 'revision': row.get('revision', 0), **result})
            row['reviews'] = reviews[-48:]
            row['supervision_status'] = ('in_progress' if result.get('phase') == 'progress' else result['decision'])
            self._save(row)

    def record_attempt(self, work_id, task_id, *, success, time_ms=None, cost=None, reason=''):
        """Count each completed execution once; progress and notification reads are free."""
        import math
        with self._lock:
            row = deepcopy(self._records[work_id])
            budget = row.setdefault('execution_budget', {'attempts': {}, 'consecutive_failures': 0,
                'elapsed_ms': 0, 'known_cost': 0, 'unknown_cost_attempts': 0,
                'failure_limit': 3, 'time_limit_ms': 600000})
            if task_id in budget['attempts']:
                return
            def number(value):
                return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
            duration, price = number(time_ms), number(cost)
            budget['attempts'][task_id] = {'success': bool(success), 'time_ms': duration,
                'cost': price, 'reason': str(reason)[:600]}
            budget['elapsed_ms'] += duration or 0
            budget['known_cost'] += price or 0
            budget['unknown_cost_attempts'] += price is None
            budget['consecutive_failures'] = 0 if success else budget['consecutive_failures'] + 1
            budget['last_reason'] = str(reason)[:600]
            budget['stop_reason'] = ('连续执行未通过达到3次，停止后续尝试' if budget['consecutive_failures'] >= budget['failure_limit'] else
                '累计已报告执行耗时达到10分钟，停止后续尝试' if budget['elapsed_ms'] >= budget['time_limit_ms'] else '')
            self._save(row)

    @staticmethod
    def budget_note(row):
        budget = (row or {}).get('execution_budget')
        if not budget:
            return ''
        return (f"已结束执行{len(budget['attempts'])}次，连续未通过{budget['consecutive_failures']}次；"
                f"累计已报告执行耗时{budget['elapsed_ms']/1000:.1f}秒。"
                + (budget.get('stop_reason') or '后续执行仍受3次连续失败、累计10分钟限制。')
                + '最近执行说明：' + budget.get('last_reason', '')
                + (f"另有{budget['unknown_cost_attempts']}次未报告费用，无法确认总费用。" if budget['unknown_cost_attempts'] else '')
                + '已有成果和未完成要求保留；停止尝试不表示任务完成。'
                + ('停止后不得承诺结果稍后自动回来，需说明原因和缺口。' if budget.get('stop_reason') else ''))

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

    def retry_interrupted_notices(self, task_ids):
        """One retry for host cancellation before cognition commits, not provider errors."""
        wanted = set(task_ids)
        with self._lock:
            for value in tuple(self._records.values()):
                row = deepcopy(value)
                changed = False
                for tid, notice in row.get('notices', {}).items():
                    if (tid in wanted and not notice['handled'] and notice.get('attempted')
                            and notice.get('interrupted_retries', 0) < 1):
                        notice.update(attempted=False, interrupted_retries=1)
                        changed = True
                if changed:
                    self._save(row)
