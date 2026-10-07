"""Durable, ordered input/output batches not yet covered by a written scene."""
from __future__ import annotations

import json
from pathlib import Path
from threading import RLock


class PendingScene:
    def __init__(self, path: Path):
        self.path = path
        self.lock = RLock()
        self.rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.rows, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)

    def append(self, subject_id: str, event_id: str, text: str, *, blocks=(), sequence=0, input_ids=(), prompt_level=1):
        with self.lock:
            if not any(row["id"] == event_id for row in self.rows):
                self.rows.append({"subject_id": subject_id, "id": event_id, "text": text, "blocks": list(blocks) or [text], "sequence": sequence, "input_ids": list(input_ids), "prompt_level": prompt_level})
                try:
                    self._save()
                except Exception:
                    self.rows.pop()
                    raise

    def snapshot(self, subject_id: str):
        with self.lock:
            return tuple(dict(row) for row in self.rows if row["subject_id"] == subject_id)

    def complete(self, ids):
        with self.lock:
            covered = set(ids)
            old = self.rows
            remaining = [row for row in old if row["id"] not in covered]
            if len(remaining) == len(old):
                return
            self.rows = remaining
            try:
                self._save()
            except Exception:
                self.rows = old
                raise

    def render(self, subject_id: str):
        return self.render_rows(self.snapshot(subject_id))

    @staticmethod
    def render_rows(rows):
        if not rows:
            return ""
        return "【尚未整理的输入输出·不占片场正文额度】\n" + "\n\n".join(row["text"] for row in rows)
