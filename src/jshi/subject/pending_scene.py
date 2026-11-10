"""Durable, ordered input/output batches not yet covered by a written scene."""
from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from copy import deepcopy


class PendingScene:
    def __init__(self, path: Path):
        self.path = path
        self.lock = RLock()
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        self.rows = saved.get('pending', []) if isinstance(saved, dict) else saved
        # Events outlive any one reader's pending queue. Older list files migrate
        # only on the first shared update; ordinary pending behavior is unchanged.
        self.events = saved.get('events', []) if isinstance(saved, dict) else []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        data = {'pending': self.rows, 'events': self.events} if self.events else self.rows
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
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

    def append_updates(self, subject_id, activity_id, updates, *, input_ids=(), restore=None):
        """Publish one durable event to both scene consumers, without re-input."""
        from time import time
        from datetime import datetime, timezone, timedelta
        from jshi.core.scene_updates import parse_scene_updates, render_update
        updates = parse_scene_updates(updates)
        if not updates:
            return
        now = time() * 1000
        recorded = datetime.fromtimestamp(now / 1000, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        with self.lock:
            old_rows, old_events = self.rows, self.events
            self.rows, self.events = list(old_rows), list(old_events)
            for n, update in enumerate(updates):
                event_id = f'{activity_id}:understanding:{n}'
                if any(row['id'] == event_id for row in self.events):
                    continue
                # Model-local P codes become stable display names before sharing.
                restored = {k: restore(v) if restore else v for k, v in update.items()}
                text = render_update(restored)
                event = {'id': event_id, 'subject_id': subject_id, 'kind': 'understanding_update',
                         'source': 'main_cognition', 'at': now, 'text': text,
                         'update': restored, 'input_ids': list(input_ids)}
                self.events.append(event)
                self.rows.append({'subject_id': subject_id, 'id': event_id, 'text': text,
                                  'blocks': [text], 'input_ids': list(input_ids), 'sequence': 0,
                                  'prompt_level': 1, 'recorded_at': recorded})
            try:
                self._save()
            except Exception:
                self.rows, self.events = old_rows, old_events
                raise

    def shared_events(self, subject_id, *, since_ms=0):
        with self.lock:
            return tuple(deepcopy(row) for row in self.events
                         if row['subject_id'] == subject_id and row['at'] >= since_ms)

    def shared_revision(self, subject_id, *, since_ms=0):
        with self.lock:
            return tuple(row['id'] for row in self.events
                         if row['subject_id'] == subject_id and row['at'] >= since_ms)

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
