"""记挂账本（210）：建任务、写反馈、按对象列出、注销。

只收阶段性事实，不包装、不策划。``visible`` / ``summary`` 由 200 写回。
读旧键 ``notify_caller`` 当作 ``visible``。存储与 ``subject.sqlite3`` 分开。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contract import (
    FeedbackKind,
    ToolEstimate,
    ToolFeedback,
    ToolProgress,
    ToolResult,
    ToolStatus,
    new_id,
    utc_now,
)

OPEN_LIST_CAP = 8
NOTE_MAX_CHARS = 80
NEED_MIN_CHARS = 2


@dataclass(frozen=True)
class HangRecord:
    task_id: str
    request_id: str
    subject_id: str
    object_id: str
    origin: str = "external_05"
    status: str = "open"  # open | notified（终态可见包装后）| cancelled
    need: str = ""
    template: str = ""
    field_ref: Mapping[str, str] = field(default_factory=dict)
    note: str = ""
    feedback: tuple[ToolFeedback, ...] = ()
    visible: bool = False
    summary: str = ""
    wrap_meta: Mapping[str, str] = field(default_factory=dict)
    meta: Mapping[str, str] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utc_now)


def truncate_note(need: str, limit: int = NOTE_MAX_CHARS) -> str:
    text = (need or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit]


def is_stage_fact(item: ToolFeedback) -> bool:
    """有内容的阶段性成果或终态。无文本的 start 心跳不算。"""
    if item.kind is FeedbackKind.RESULT and item.result is not None:
        return True
    if item.kind is FeedbackKind.PROGRESS and item.progress is not None:
        return bool((item.progress.partial or "").strip())
    return False


def rule_wrap_from_item(item: ToolFeedback) -> tuple[bool, str] | None:
    """无包装 skill 时的占位：非拒绝的 RESULT → 可见短句。"""
    if item.kind is not FeedbackKind.RESULT or item.result is None:
        return None
    if item.result.status is ToolStatus.REJECTED:
        return None
    text = (item.result.summary or item.result.error or "").strip()
    if not text:
        text = item.result.status.value
    return True, truncate_note(text)


def _feedback_to_dict(item: ToolFeedback) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_id": item.event_id,
        "request_id": item.request_id,
        "kind": item.kind.value,
        "at": item.at.isoformat(),
        "version": item.version,
        "meta": dict(item.meta),
    }
    if item.estimate is not None:
        estimate = item.estimate
        payload["estimate"] = {
            "cost_est": estimate.cost_est,
            "time_est_ms": estimate.time_est_ms,
            "resource_est": dict(estimate.resource_est),
            "benefit": estimate.benefit,
            "downside": estimate.downside,
            "need_confirm": estimate.need_confirm,
        }
    if item.progress is not None:
        progress = item.progress
        payload["progress"] = {
            "stage": progress.stage,
            "step": progress.step,
            "partial": progress.partial,
            "cost_used": progress.cost_used,
            "time_used_ms": progress.time_used_ms,
            "resource_used": dict(progress.resource_used),
            "note": progress.note,
        }
    if item.result is not None:
        result = item.result
        payload["result"] = {
            "status": result.status.value,
            "result": dict(result.result),
            "summary": result.summary,
            "ideal": result.ideal,
            "ideal_note": result.ideal_note,
            "cost": result.cost,
            "time_ms": result.time_ms,
            "resource": dict(result.resource),
            "execution": [dict(step) for step in result.execution],
            "error": result.error,
        }
    return payload


def _dt(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    text = str(raw or "").strip()
    if not text:
        return utc_now()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return utc_now()


def _feedback_from_dict(data: Mapping[str, Any]) -> ToolFeedback:
    estimate = None
    raw_est = data.get("estimate")
    if isinstance(raw_est, Mapping):
        estimate = ToolEstimate(
            cost_est=raw_est.get("cost_est"),
            time_est_ms=raw_est.get("time_est_ms"),
            resource_est=dict(raw_est.get("resource_est") or {}),
            benefit=str(raw_est.get("benefit") or ""),
            downside=str(raw_est.get("downside") or ""),
            need_confirm=bool(raw_est.get("need_confirm", False)),
        )
    progress = None
    raw_prog = data.get("progress")
    if isinstance(raw_prog, Mapping):
        progress = ToolProgress(
            stage=str(raw_prog.get("stage") or ""),
            step=str(raw_prog.get("step") or ""),
            partial=str(raw_prog.get("partial") or ""),
            cost_used=raw_prog.get("cost_used"),
            time_used_ms=raw_prog.get("time_used_ms"),
            resource_used=dict(raw_prog.get("resource_used") or {}),
            note=str(raw_prog.get("note") or ""),
        )
    result = None
    raw_res = data.get("result")
    if isinstance(raw_res, Mapping):
        status_raw = str(raw_res.get("status") or ToolStatus.FAILED.value)
        try:
            status = ToolStatus(status_raw)
        except ValueError:
            status = ToolStatus.FAILED
        result = ToolResult(
            status=status,
            result=dict(raw_res.get("result") or {}),
            summary=str(raw_res.get("summary") or ""),
            ideal=bool(raw_res.get("ideal", False)),
            ideal_note=str(raw_res.get("ideal_note") or ""),
            cost=raw_res.get("cost"),
            time_ms=raw_res.get("time_ms"),
            resource=dict(raw_res.get("resource") or {}),
            execution=tuple(
                dict(step) for step in (raw_res.get("execution") or ()) if isinstance(step, Mapping)
            ),
            error=str(raw_res.get("error") or ""),
        )
    kind_raw = str(data.get("kind") or FeedbackKind.RESULT.value)
    try:
        kind = FeedbackKind(kind_raw)
    except ValueError:
        kind = FeedbackKind.RESULT
    return ToolFeedback(
        event_id=str(data.get("event_id") or new_id()),
        request_id=str(data.get("request_id") or ""),
        kind=kind,
        at=_dt(data.get("at")),
        version=str(data.get("version") or "v1"),
        estimate=estimate,
        progress=progress,
        result=result,
        meta=dict(data.get("meta") or {}) if isinstance(data.get("meta"), Mapping) else {},
    )


def _record_to_dict(record: HangRecord) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "request_id": record.request_id,
        "subject_id": record.subject_id,
        "object_id": record.object_id,
        "origin": record.origin,
        "status": record.status,
        "need": record.need,
        "template": record.template,
        "field_ref": dict(record.field_ref),
        "note": record.note,
        "feedback": [_feedback_to_dict(item) for item in record.feedback],
        "visible": record.visible,
        "summary": record.summary,
        "wrap_meta": dict(record.wrap_meta),
        "meta": dict(record.meta),
        "updated_at": record.updated_at.isoformat(),
    }


def _record_from_dict(data: Mapping[str, Any]) -> HangRecord:
    feedback = tuple(
        _feedback_from_dict(item)
        for item in (data.get("feedback") or ())
        if isinstance(item, Mapping)
    )
    field_ref = data.get("field_ref") or {}
    if not isinstance(field_ref, Mapping):
        field_ref = {}
    wrap_meta = data.get("wrap_meta") or {}
    if not isinstance(wrap_meta, Mapping):
        wrap_meta = {}
    meta = data.get("meta") or {}
    if not isinstance(meta, Mapping):
        meta = {}
    visible = bool(data.get("visible", data.get("notify_caller", False)))
    return HangRecord(
        task_id=str(data.get("task_id") or new_id()),
        request_id=str(data.get("request_id") or ""),
        subject_id=str(data.get("subject_id") or ""),
        object_id=str(data.get("object_id") or ""),
        origin=str(data.get("origin") or "external_05"),
        status=str(data.get("status") or "open"),
        need=str(data.get("need") or ""),
        template=str(data.get("template") or ""),
        field_ref={str(key): str(value) for key, value in field_ref.items()},
        note=str(data.get("note") or ""),
        feedback=feedback,
        visible=visible,
        summary=str(data.get("summary") or ""),
        wrap_meta={str(key): str(value) for key, value in wrap_meta.items()},
        meta={str(key): str(value) for key, value in meta.items()},
        updated_at=_dt(data.get("updated_at")),
    )


class HangStore:
    """jsonl 记挂账本。崩溃可丢进行中的子进程，账本行仍在。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, HangRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        text = self.path.read_text(encoding="utf-8")
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            record = _record_from_dict(data)
            self._records[record.task_id] = record

    def _append(self, record: HangRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_record_to_dict(record), ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def get(self, task_id: str) -> HangRecord | None:
        with self._lock:
            return self._records.get(task_id)

    def create(
        self,
        *,
        subject_id: str,
        object_id: str,
        need: str,
        template: str = "",
        field_ref: Mapping[str, str] | None = None,
        origin: str = "external_05",
        request_id: str = "",
        task_id: str = "",
        meta: Mapping[str, str] | None = None,
    ) -> HangRecord:
        record = HangRecord(
            task_id=task_id or new_id(),
            request_id=request_id or new_id(),
            subject_id=subject_id,
            object_id=object_id,
            origin=origin or "external_05",
            status="open",
            need=need,
            template=template,
            field_ref=dict(field_ref or {}),
            note=truncate_note(need),
            feedback=(),
            visible=False,
            summary="",
            wrap_meta={},
            meta=dict(meta or {}),
            updated_at=utc_now(),
        )
        with self._lock:
            self._records[record.task_id] = record
            self._append(record)
        return record

    def append_feedback(
        self, task_id: str, items: Sequence[ToolFeedback]
    ) -> HangRecord | None:
        incoming = tuple(items)
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return None
            merged = record.feedback + incoming
            updated = replace(record, feedback=merged, updated_at=utc_now())
            self._records[task_id] = updated
            self._append(updated)
            return updated

    def set_wrap(
        self,
        task_id: str,
        *,
        visible: bool,
        summary: str,
        wrap_meta: Mapping[str, str] | None = None,
        terminal: bool = False,
    ) -> HangRecord | None:
        summary = truncate_note(summary)
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return None
            if record.status == "cancelled":
                updated = replace(
                    record,
                    summary=summary,
                    wrap_meta=dict(wrap_meta or record.wrap_meta),
                    updated_at=utc_now(),
                )
            else:
                status = record.status
                if visible and terminal:
                    status = "notified"
                updated = replace(
                    record,
                    visible=visible,
                    summary=summary if visible else record.summary,
                    wrap_meta=dict(wrap_meta or {}),
                    status=status,
                    updated_at=utc_now(),
                )
            self._records[task_id] = updated
            self._append(updated)
            return updated

    def list_for(self, subject_id: str, object_id: str) -> tuple[HangRecord, ...]:
        with self._lock:
            items = [
                record
                for record in self._records.values()
                if record.subject_id == subject_id and record.object_id == object_id
            ]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(items)

    def list_open(self, subject_id: str, object_id: str) -> tuple[HangRecord, ...]:
        """仍为 open 的账本。主流程用 ``list_visible``；本口留给测试与巡检。"""
        with self._lock:
            items = [
                record
                for record in self._records.values()
                if record.subject_id == subject_id
                and record.object_id == object_id
                and record.status == "open"
            ]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(items[:OPEN_LIST_CAP])

    def cancel(self, task_id: str) -> HangRecord | None:
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return None
            updated = replace(record, status="cancelled", updated_at=utc_now())
            self._records[task_id] = updated
            self._append(updated)
            return updated
