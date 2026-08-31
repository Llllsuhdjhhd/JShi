"""09 薄壳：召回策略快照。不进 MemoryBackendPort，只改后续 recall 入参。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _clamp_level(value: int) -> int:
    return min(max(int(value), 1), 9)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RecallStrategy:
    default_level: int = 1
    limit: int | None = None
    source_report_id: str = ""
    updated_at: datetime | None = None


class RecallStrategyStore:
    """主体侧 json：`{data_dir}/recall_strategy.json`。无文件则每主体默认档位 1。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._entries: dict[str, RecallStrategy] = {}
        if self._path is not None and self._path.exists():
            self._load()

    def get(self, subject_id: str) -> RecallStrategy:
        return self._entries.get(subject_id, RecallStrategy())

    def apply(
        self,
        subject_id: str,
        *,
        default_level: int,
        limit: int | None = None,
        source_report_id: str = "",
    ) -> RecallStrategy:
        strategy = RecallStrategy(
            default_level=_clamp_level(default_level),
            limit=limit,
            source_report_id=source_report_id,
            updated_at=_utc_now(),
        )
        self._entries[subject_id] = strategy
        self._save()
        return strategy

    def _load(self) -> None:
        assert self._path is not None
        raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        if not isinstance(raw, dict):
            return
        for subject_id, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            updated = payload.get("updated_at") or ""
            try:
                updated_at = datetime.fromisoformat(updated) if updated else None
            except ValueError:
                updated_at = None
            limit = payload.get("limit")
            self._entries[str(subject_id)] = RecallStrategy(
                default_level=_clamp_level(payload.get("default_level", 1) or 1),
                limit=int(limit) if limit is not None else None,
                source_report_id=str(payload.get("source_report_id") or ""),
                updated_at=updated_at,
            )

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            subject_id: {
                "default_level": strategy.default_level,
                "limit": strategy.limit,
                "source_report_id": strategy.source_report_id,
                "updated_at": (
                    strategy.updated_at.isoformat() if strategy.updated_at else ""
                ),
            }
            for subject_id, strategy in self._entries.items()
        }
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
