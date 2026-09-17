"""交接账本：intake 保存调度上下文，不是记挂。

与 hang.jsonl 分开，默认 ``{data-dir}/tool.jsonl``。不进 subject.sqlite3。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .contract import new_id, utc_now
from .stage import ToolStageEvent


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

ORIGIN_EXTERNAL_05 = "external_05"


@dataclass(frozen=True)
class IntakeRecord:
    intake_id: str
    subject_id: str
    object_id: str
    activity_id: str = ""
    origin: str = ORIGIN_EXTERNAL_05
    need: str = ""
    verbal: str = ""
    field_ref: Mapping[str, str] = field(default_factory=dict)
    status: str = "received"  # received | failed | launched | cancelled
    plan_error: str = ""
    request_id: str = ""
    task_id: str = ""
    meta: Mapping[str, str] = field(default_factory=dict)
    stages: tuple[ToolStageEvent, ...] = ()
    # 交付：策划失败句也已经写进该对象的片场。非空后不再走 tool_input。
    delivered_at: datetime | None = None
    delivered_block: str = ""
    updated_at: datetime = field(default_factory=utc_now)


def _record_to_dict(record: IntakeRecord) -> dict[str, Any]:
    return {
        "intake_id": record.intake_id,
        "subject_id": record.subject_id,
        "object_id": record.object_id,
        "activity_id": record.activity_id,
        "origin": record.origin,
        "need": record.need,
        "verbal": record.verbal,
        "field_ref": dict(record.field_ref),
        "status": record.status,
        "plan_error": record.plan_error,
        "request_id": record.request_id,
        "task_id": record.task_id,
        "meta": dict(record.meta),
        "stages": [stage.as_dict() for stage in record.stages],
        "delivered_at": record.delivered_at.isoformat() if record.delivered_at else "",
        "delivered_block": record.delivered_block,
        "updated_at": record.updated_at.isoformat(),
    }


def _record_from_dict(data: Mapping[str, Any]) -> IntakeRecord:
    field_ref = data.get("field_ref") or {}
    if not isinstance(field_ref, Mapping):
        field_ref = {}
    return IntakeRecord(
        intake_id=str(data.get("intake_id") or new_id()),
        subject_id=str(data.get("subject_id") or ""),
        object_id=str(data.get("object_id") or ""),
        activity_id=str(data.get("activity_id") or ""),
        origin=str(data.get("origin") or ORIGIN_EXTERNAL_05),
        need=str(data.get("need") or ""),
        verbal=str(data.get("verbal") or ""),
        field_ref={str(key): str(value) for key, value in field_ref.items()},
        status=str(data.get("status") or "received"),
        plan_error=str(data.get("plan_error") or ""),
        request_id=str(data.get("request_id") or ""),
        task_id=str(data.get("task_id") or ""),
        meta={
            str(key): str(value)
            for key, value in (data.get("meta") or {}).items()
        }
        if isinstance(data.get("meta"), Mapping)
        else {},
        stages=tuple(
            ToolStageEvent.from_dict(item)
            for item in (data.get("stages") or ())
            if isinstance(item, Mapping)
        ),
        delivered_at=(
            _dt(data["delivered_at"]) if str(data.get("delivered_at") or "").strip() else None
        ),
        delivered_block=str(data.get("delivered_block") or ""),
        updated_at=_dt(data.get("updated_at")),
    )


class IntakeStore:
    """jsonl 交接账本。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, IntakeRecord] = {}
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
            self._records[record.intake_id] = record

    def _append(self, record: IntakeRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_record_to_dict(record), ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def get(self, intake_id: str) -> IntakeRecord | None:
        with self._lock:
            return self._records.get(intake_id)

    def get_by_task_id(self, task_id: str) -> IntakeRecord | None:
        if not task_id:
            return None
        with self._lock:
            for record in self._records.values():
                if record.task_id == task_id:
                    return record
        return None

    def create(
        self,
        *,
        subject_id: str,
        object_id: str,
        activity_id: str = "",
        need: str = "",
        verbal: str = "",
        field_ref: Mapping[str, str] | None = None,
        origin: str = ORIGIN_EXTERNAL_05,
        intake_id: str = "",
    ) -> IntakeRecord:
        record = IntakeRecord(
            intake_id=intake_id or new_id(),
            subject_id=subject_id,
            object_id=object_id,
            activity_id=activity_id,
            origin=origin or ORIGIN_EXTERNAL_05,
            need=need,
            verbal=verbal,
            field_ref=dict(field_ref or {}),
            status="received",
            updated_at=utc_now(),
        )
        with self._lock:
            self._records[record.intake_id] = record
            self._append(record)
        return record

    def update(self, intake_id: str, **changes: Any) -> IntakeRecord | None:
        with self._lock:
            record = self._records.get(intake_id)
            if record is None:
                return None
            updated = replace(record, updated_at=utc_now(), **changes)
            self._records[intake_id] = updated
            self._append(updated)
            return updated

    def append_stage(
        self, intake_id: str, stage: ToolStageEvent
    ) -> IntakeRecord | None:
        with self._lock:
            record = self._records.get(intake_id)
            if record is None:
                return None
            updated = replace(
                record,
                stages=record.stages + (stage,),
                updated_at=utc_now(),
            )
            self._records[intake_id] = updated
            self._append(updated)
            return updated

    def list_for(self, subject_id: str, object_id: str) -> tuple[IntakeRecord, ...]:
        with self._lock:
            items = [
                record
                for record in self._records.values()
                if record.subject_id == subject_id and record.object_id == object_id
            ]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(items)

    def list_for_subject(self, subject_id: str) -> tuple[IntakeRecord, ...]:
        with self._lock:
            items = [
                record
                for record in self._records.values()
                if record.subject_id == subject_id
            ]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(items)

    def set_delivered(
        self, intake_id: str, *, block: str = ""
    ) -> IntakeRecord | None:
        """记：这条失败句已写进该对象的片场。之后不再走 ``tool_input``。"""
        text = (block or "").strip()
        with self._lock:
            record = self._records.get(intake_id)
            if record is None:
                return None
            updated = replace(
                record,
                delivered_at=utc_now(),
                delivered_block=text or record.delivered_block,
                updated_at=utc_now(),
            )
            self._records[intake_id] = updated
            self._append(updated)
            return updated

    def clear_delivered(self, intake_id: str) -> IntakeRecord | None:
        """撤销交付标记（片场这轮没接下），让它继续走 ``tool_input``。"""
        with self._lock:
            record = self._records.get(intake_id)
            if record is None:
                return None
            updated = replace(record, delivered_at=None, updated_at=utc_now())
            self._records[intake_id] = updated
            self._append(updated)
            return updated

    def cancel(self, intake_id: str) -> IntakeRecord | None:
        return self.update(intake_id, status="cancelled")
