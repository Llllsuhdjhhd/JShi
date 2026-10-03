"""活动耗时落盘。落盘之前的轮次没有记录。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class TimingRow:
    subject_id: str
    activity_id: str
    started_at: datetime
    total_ms: float


def append_timing(
    path: Path | str,
    *,
    subject_id: str,
    activity_id: str,
    started_at: str,
    total_ms: float,
    steps: tuple[tuple[str, float], ...],
) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "subject_id": subject_id,
        "activity_id": activity_id,
        "started_at": started_at,
        "total_ms": total_ms,
        "steps": [[name, ms] for name, ms in steps],
    }
    with file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
