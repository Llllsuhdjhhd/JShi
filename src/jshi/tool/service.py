"""200 对外口：intake / list_visible / cancel。

主流程与 03 只调这三口。策划在后台；发出 ToolRequest 之后才建记挂。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .contract import (
    FeedbackKind,
    ToolEstimate,
    ToolFeedback,
    ToolRequest,
    utc_now,
)
from .hang import (
    HangStore,
    OPEN_LIST_CAP,
    is_stage_fact,
    rule_wrap_from_item,
    truncate_note,
)
from .intake import IntakeRecord, IntakeStore
from .plan import PlanFailure, RulePlanner, ToolPlanner
from .port import ToolRunner

logger = logging.getLogger(__name__)

PLACEHOLDER_DOWNSIDE = "占位估价；内容由引擎执行"


@dataclass(frozen=True)
class VisibleToolItem:
    """03 可见条目。``summary`` 是交给 03 的正文，组装原样装入，不改写。"""

    id: str
    summary: str
    subject_id: str
    object_id: str
    kind: str  # plan_failed | hang
    updated_at: datetime = field(default_factory=utc_now)


WRAP_CONCURRENCY = 2


class ToolService:
    """200 门面。可重入：每次 intake 各开后台，不拿一把全局锁堵住第二次。"""

    def __init__(
        self,
        hang_store: HangStore,
        runner: ToolRunner,
        *,
        intake_store: IntakeStore | None = None,
        planner: ToolPlanner | None = None,
        wrap_skill: Any | None = None,
        intake_path: str | Path | None = None,
        wrap_concurrency: int = WRAP_CONCURRENCY,
    ) -> None:
        self.hang_store = hang_store
        self.runner = runner
        if intake_store is not None:
            self.intake_store = intake_store
        else:
            path = Path(intake_path) if intake_path is not None else hang_store.path.parent / "tool.jsonl"
            self.intake_store = IntakeStore(path)
        self.planner = planner or RulePlanner()
        self.wrap_skill = wrap_skill
        self._lock = threading.Lock()
        self._planner_threads: list[threading.Thread] = []
        self._wrap_slots = threading.Semaphore(max(1, wrap_concurrency))
        self._wrap_lock = threading.Lock()
        self._wrap_pending: dict[str, list] = {}
        self._wrap_workers: dict[str, threading.Thread] = {}
        self._wrap_threads: list[threading.Thread] = []
        self.runner.on_feedback = self._on_engine_feedback

    def intake(
        self,
        *,
        subject_id: str,
        object_id: str,
        activity_id: str = "",
        need: str = "",
        verbal: str = "",
        field_ref: Mapping[str, str] | None = None,
        origin: str = "external_05",
    ) -> IntakeRecord:
        record = self.intake_store.create(
            subject_id=subject_id,
            object_id=object_id,
            activity_id=activity_id,
            need=need,
            verbal=verbal,
            field_ref=field_ref,
            origin=origin,
        )
        thread = threading.Thread(
            target=self._plan_and_launch,
            args=(record.intake_id,),
            name=f"tool-plan-{record.intake_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._planner_threads.append(thread)
        thread.start()
        return record

    def _plan_and_launch(self, intake_id: str) -> None:
        record = self.intake_store.get(intake_id)
        if record is None or record.status == "cancelled":
            return
        try:
            planned = self.planner.plan(record)
        except Exception:
            logger.exception("tool planner failed")
            self.intake_store.update(
                intake_id,
                status="failed",
                plan_error="策划出错",
            )
            return
        current = self.intake_store.get(intake_id)
        if current is None or current.status == "cancelled":
            return
        if isinstance(planned, PlanFailure):
            self.intake_store.update(
                intake_id,
                status="failed",
                plan_error=planned.message,
            )
            return
        self._launch(current, planned)

    def _launch(self, intake: IntakeRecord, request: ToolRequest) -> None:
        self.intake_store.update(intake.intake_id, status="planned")
        hang = self.hang_store.create(
            subject_id=intake.subject_id,
            object_id=intake.object_id,
            need=request.need or intake.need,
            template=request.template or "",
            field_ref=intake.field_ref,
            origin=intake.origin,
            request_id=request.request_id,
        )
        bound = replace(
            request,
            request_id=hang.request_id,
            subject_id=intake.subject_id,
            activity_id=intake.activity_id or request.activity_id,
        )
        est_meta = bound.meta.get("estimate") if isinstance(bound.meta, Mapping) else {}
        if not isinstance(est_meta, Mapping):
            est_meta = {}
        estimate = ToolFeedback(
            request_id=hang.request_id,
            kind=FeedbackKind.ESTIMATE,
            estimate=ToolEstimate(
                benefit=str(est_meta.get("benefit") or "").strip()
                or truncate_note(bound.need or intake.need),
                downside=str(est_meta.get("downside") or "").strip()
                or PLACEHOLDER_DOWNSIDE,
                time_est_ms=est_meta.get("time_est_ms"),
                cost_est=est_meta.get("cost_est"),
                need_confirm=bool(est_meta.get("need_confirm", False)),
            ),
        )
        self.hang_store.append_feedback(hang.task_id, (estimate,))
        self.intake_store.update(
            intake.intake_id,
            status="launched",
            request_id=hang.request_id,
            task_id=hang.task_id,
        )
        self.runner.start(bound, hang.task_id)

    def _on_engine_feedback(self, task_id: str, item: ToolFeedback) -> None:
        if not is_stage_fact(item):
            return
        if self.wrap_skill is None:
            wrapped = rule_wrap_from_item(item)
            if wrapped is None:
                return
            visible, summary = wrapped
            self.hang_store.set_wrap(task_id, visible=visible, summary=summary)
            return
        with self._wrap_lock:
            self._wrap_pending.setdefault(task_id, []).append(item)
            worker = self._wrap_workers.get(task_id)
            if worker is not None and worker.is_alive():
                return
            thread = threading.Thread(
                target=self._wrap_loop,
                args=(task_id,),
                name=f"tool-wrap-{task_id[:8]}",
                daemon=True,
            )
            self._wrap_workers[task_id] = thread
            self._wrap_threads.append(thread)
            thread.start()

    def _wrap_loop(self, task_id: str) -> None:
        while True:
            with self._wrap_lock:
                batch = self._wrap_pending.pop(task_id, [])
                if not batch:
                    if self._wrap_workers.get(task_id) is threading.current_thread():
                        self._wrap_workers.pop(task_id, None)
                    return
            self._wrap_slots.acquire()
            try:
                self._run_wrap(task_id, batch)
            finally:
                self._wrap_slots.release()

    def _run_wrap(self, task_id: str, batch: list) -> None:
        hang = self.hang_store.get(task_id)
        if hang is None or hang.status == "cancelled":
            return
        skill = self.wrap_skill
        if skill is None:
            return
        try:
            result = skill.wrap_hang(hang, batch)
        except Exception:
            logger.exception("tool wrap skill failed")
            result = None
        if result is None:
            terminal = next(
                (
                    item.result
                    for item in reversed(batch)
                    if item.kind is FeedbackKind.RESULT and item.result is not None
                ),
                None,
            )
            if terminal is None:
                return
            text = (terminal.error or terminal.status.value).strip()
            if not text:
                return
            self.hang_store.set_wrap(
                task_id,
                visible=True,
                summary=truncate_note(text),
                wrap_meta={"source": "fallback"},
            )
            return
        self.hang_store.set_wrap(
            task_id,
            visible=bool(result.visible) and bool((result.summary or "").strip()),
            summary=(result.summary or "").strip(),
            wrap_meta={"model_tag": getattr(skill, "model_tag", "")},
        )

    def list_visible(
        self, subject_id: str, object_id: str
    ) -> tuple[VisibleToolItem, ...]:
        items: list[VisibleToolItem] = []
        for record in self.intake_store.list_for(subject_id, object_id):
            if record.status != "failed":
                continue
            items.append(
                VisibleToolItem(
                    id=record.intake_id,
                    summary=(record.plan_error or "策划失败").strip(),
                    subject_id=record.subject_id,
                    object_id=record.object_id,
                    kind="plan_failed",
                    updated_at=record.updated_at,
                )
            )
        for hang in self.hang_store.list_for(subject_id, object_id):
            if hang.status == "cancelled" or not hang.visible:
                continue
            summary = (hang.summary or "").strip()
            if not summary:
                continue
            items.append(
                VisibleToolItem(
                    id=hang.task_id,
                    summary=summary,
                    subject_id=hang.subject_id,
                    object_id=hang.object_id,
                    kind="hang",
                    updated_at=hang.updated_at,
                )
            )
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(items[:OPEN_LIST_CAP])

    def cancel(self, item_id: str) -> bool:
        found = False
        hang = self.hang_store.get(item_id)
        if hang is not None:
            self.hang_store.cancel(item_id)
            found = True
            twin = self.intake_store.get_by_task_id(item_id)
            if twin is not None:
                self.intake_store.cancel(twin.intake_id)
        intake = self.intake_store.get(item_id)
        if intake is not None:
            self.intake_store.cancel(item_id)
            found = True
            if intake.task_id:
                self.hang_store.cancel(intake.task_id)
        return found

    def drain_planning_for_tests(self, timeout: float = 5.0) -> None:
        with self._lock:
            threads = list(self._planner_threads)
            self._planner_threads.clear()
        for thread in threads:
            thread.join(timeout)

    def drain_for_tests(self, timeout: float = 5.0) -> None:
        self.drain_planning_for_tests(timeout)
        self.runner.drain_for_tests(timeout)
        with self._wrap_lock:
            threads = list(self._wrap_threads)
            self._wrap_threads.clear()
        for thread in threads:
            thread.join(timeout)
