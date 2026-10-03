"""重放用的模型输入。系统提示词按哈希只存一次。主路径不读这个文件。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from jshi.core.params import step_inputs_max_bytes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class StepInputStore:
    """含完整对话，不外传。超出上限后按调用时间删最旧的一行。"""

    def __init__(self, path: Path | str, *, max_bytes: int | None = None) -> None:
        self.path = Path(path)
        self._max_bytes = max_bytes

    def _limit(self) -> int:
        if self._max_bytes is not None:
            return self._max_bytes
        return step_inputs_max_bytes()

    def append_call(
        self,
        *,
        subject_id: str,
        activity_id: str,
        purpose: str,
        model: str,
        system_text: str,
        user_text: str,
    ) -> None:
        if purpose not in {"subject_activity", "write_zone"}:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        digest = _hash(system_text)
        existing = self._read()
        if not any(
            row.get("kind") == "prompt" and row.get("hash") == digest for row in existing
        ):
            self._append(
                {"kind": "prompt", "hash": digest, "text": system_text}
            )
        self._append(
            {
                "kind": "call",
                "subject_id": subject_id,
                "activity_id": activity_id,
                "purpose": purpose,
                "model": model,
                "system_hash": digest,
                "user_text": user_text,
                "at": _now(),
            }
        )
        if self.path.stat().st_size > self._limit():
            self._compact()

    def _append(self, payload: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except ValueError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows

    def _compact(self) -> None:
        rows = self._read()
        prompts = [row for row in rows if row.get("kind") == "prompt"]
        calls = [row for row in rows if row.get("kind") == "call"]
        target = self._limit() // 2

        def size(items: list[dict]) -> int:
            return sum(
                len((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
                for item in items
            )

        while calls and size(prompts) + size(calls) > target:
            calls.pop(0)
        used = {row.get("system_hash") for row in calls}
        prompts = [row for row in prompts if row.get("hash") in used]
        body = "".join(
            json.dumps(item, ensure_ascii=False) + "\n" for item in (*prompts, *calls)
        )
        self.path.write_text(body, encoding="utf-8")
