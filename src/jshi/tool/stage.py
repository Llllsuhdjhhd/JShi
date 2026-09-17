"""工具过程统一阶段事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .contract import new_id, utc_now

PHASE_PLANNING = "planning"
PHASE_ESTIMATE = "estimate"
PHASE_PROGRESS = "progress"
PHASE_RESULT = "result"
PHASE_CANCEL = "cancel"
PHASE_WRAP = "wrap"


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


@dataclass(frozen=True)
class ToolStageEvent:
    id: str
    task_id: str
    phase: str
    kind: str = ""
    status: str = ""
    text: str = ""
    command: str = ""
    stage_name: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "phase": self.phase,
            "kind": self.kind,
            "status": self.status,
            "text": self.text,
            "command": self.command,
            "stage_name": self.stage_name,
            "metrics": dict(self.metrics),
            "meta": dict(self.meta),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolStageEvent":
        metrics = data.get("metrics") or {}
        meta = data.get("meta") or {}
        return cls(
            id=str(data.get("id") or new_id()),
            task_id=str(data.get("task_id") or ""),
            phase=str(data.get("phase") or ""),
            kind=str(data.get("kind") or ""),
            status=str(data.get("status") or ""),
            text=str(data.get("text") or ""),
            command=str(data.get("command") or ""),
            stage_name=str(data.get("stage_name") or ""),
            metrics=dict(metrics) if isinstance(metrics, Mapping) else {},
            meta=dict(meta) if isinstance(meta, Mapping) else {},
            created_at=_dt(data.get("created_at")),
        )


def stage_event(
    task_id: str,
    phase: str,
    *,
    kind: str = "",
    status: str = "",
    text: str = "",
    command: str = "",
    stage_name: str = "",
    metrics: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> ToolStageEvent:
    return ToolStageEvent(
        id=new_id(),
        task_id=task_id,
        phase=phase,
        kind=kind,
        status=status,
        text=text,
        command=command,
        stage_name=stage_name,
        metrics=dict(metrics or {}),
        meta=dict(meta or {}),
    )

