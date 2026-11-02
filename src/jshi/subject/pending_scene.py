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

    def append(self, subject_id: str, event_id: str, text: str, *, blocks=(), sequence=0, input_ids=(), prompt_level=1, recorded_at=""):
        if not recorded_at:
            from datetime import datetime, timezone, timedelta
            import re
            match = re.match(r"^\[([^\]]+)\]", text)
            if match:
                try:
                    parsed = datetime.fromisoformat(match.group(1))
                    if parsed.tzinfo is not None:
                        recorded_at = parsed.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    pass
        with self.lock:
            if not any(row["id"] == event_id for row in self.rows):
                self.rows.append({"subject_id": subject_id, "id": event_id, "text": text, "blocks": list(blocks) or [text], "sequence": sequence, "input_ids": list(input_ids), "prompt_level": prompt_level, "recorded_at": recorded_at})
                try:
                    self._save()
                except Exception:
                    self.rows.pop()
                    raise

    def snapshot(self, subject_id: str):
        with self.lock:
            return tuple(dict(row) for row in self.rows if row["subject_id"] == subject_id)

    def input_ids(self, subject_id):
        return {iid for row in self.snapshot(subject_id) for iid in row.get('input_ids', ())}

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
        from jshi.core.scene_text import observation_text
        return "【尚未整理的输入输出·不占片场正文额度】\n" + "\n\n".join(observation_text(row["text"]) for row in rows)
