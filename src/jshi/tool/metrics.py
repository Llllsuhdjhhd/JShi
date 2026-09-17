"""工具运行实测统计：按 command 累计时间 / token / 费用，供后续更新工具描述。"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contract import ToolResult


@dataclass(frozen=True)
class ToolRunStats:
    command: str
    runs: int = 0
    sum_time_ms: float = 0.0
    sum_cost: float = 0.0
    sum_tokens: float = 0.0

    @property
    def avg_time_ms(self) -> float | None:
        return self.sum_time_ms / self.runs if self.runs else None

    @property
    def avg_cost(self) -> float | None:
        return self.sum_cost / self.runs if self.runs else None

    @property
    def avg_tokens(self) -> float | None:
        return self.sum_tokens / self.runs if self.runs else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "runs": self.runs,
            "sum_time_ms": self.sum_time_ms,
            "sum_cost": self.sum_cost,
            "sum_tokens": self.sum_tokens,
            "avg_time_ms": self.avg_time_ms,
            "avg_cost": self.avg_cost,
            "avg_tokens": self.avg_tokens,
        }


def _tokens_from_result(result: ToolResult) -> float:
    values: list[float] = []
    resource = result.resource or {}
    for key in ("totalTokens", "input", "output"):
        value = resource.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    if values:
        return max(values)
    usage = result.execution[0].get("usage") if result.execution else None
    if isinstance(usage, Mapping):
        for key in ("totalTokens", "input", "output"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append(float(value))
        if values:
            return max(values)
    return 0.0


class ToolMetricsStore:
    """把每次工具终态累计成 command 级别的均值。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stats: dict[str, ToolRunStats] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, Mapping):
            return
        for raw in data.values():
            if not isinstance(raw, Mapping):
                continue
            command = str(raw.get("command") or "").strip()
            if not command:
                continue
            self._stats[command] = ToolRunStats(
                command=command,
                runs=int(raw.get("runs") or 0),
                sum_time_ms=float(raw.get("sum_time_ms") or 0.0),
                sum_cost=float(raw.get("sum_cost") or 0.0),
                sum_tokens=float(raw.get("sum_tokens") or 0.0),
            )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            command: stats.as_dict() for command, stats in self._stats.items()
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def stats(self, command: str) -> ToolRunStats:
        command = (command or "").strip()
        with self._lock:
            return self._stats.get(command, ToolRunStats(command=command))

    def record(self, command: str, result: ToolResult) -> ToolRunStats:
        command = (command or "").strip()
        if not command:
            return ToolRunStats(command=command)
        time_ms = float(result.time_ms or 0.0)
        cost = float(result.cost or 0.0)
        tokens = _tokens_from_result(result)
        with self._lock:
            old = self._stats.get(command, ToolRunStats(command=command))
            updated = ToolRunStats(
                command=command,
                runs=old.runs + 1,
                sum_time_ms=old.sum_time_ms + time_ms,
                sum_cost=old.sum_cost + cost,
                sum_tokens=old.sum_tokens + tokens,
            )
            self._stats[command] = updated
            self._save()
            return updated

