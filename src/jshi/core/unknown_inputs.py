"""Unknown speakers stay isolated; only manual input-level bindings migrate attribution."""
from pathlib import Path
import sqlite3


class UnknownInputs:
    def __init__(self, path: Path, max_bytes: int = 500 * 1024 * 1024, *, initialize=True):
        self.path, self.max_bytes = path, max_bytes
        self.read_only = not initialize
        if not initialize:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS inputs (input_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, text TEXT NOT NULL, session_id TEXT NOT NULL, start_ms INTEGER, end_ms INTEGER, manual_object_id TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS manual_person ON inputs(manual_object_id)")

    def connect(self):
        if self.read_only:
            return sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        return sqlite3.connect(self.path, timeout=10)

    def append(self, input_id, actor_id, text, session_id, start_ms, end_ms):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM inputs WHERE input_id=?", (input_id,)).fetchone():
                return True
            size = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
            if size + len(text.encode('utf-8')) + 8192 > self.max_bytes:
                return False
            db.execute("INSERT INTO inputs VALUES (?, ?, ?, ?, ?, ?, NULL)",
                       (input_id, actor_id, text, session_id, start_ms, end_ms))
            return True

    def bind(self, input_ids, object_id):
        with self.connect() as db:
            db.executemany("UPDATE inputs SET manual_object_id=? WHERE input_id=?", ((object_id, iid) for iid in input_ids))

    def read(self, input_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM inputs WHERE input_id=?", (input_id,)).fetchone()
            return dict(row) if row else None

    def for_person(self, object_id, query='', limit=6, max_chars=2000):
        """Only explicit manual bindings participate; guesses never index history."""
        import re
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute(
                "SELECT rowid AS sequence, * FROM inputs WHERE manual_object_id=? ORDER BY rowid DESC LIMIT 64",
                (object_id,))]
        terms = set(re.findall(r'[A-Za-z]{2,}|[\u4e00-\u9fff]{2}', query))
        rows.sort(key=lambda row: (sum(term in row['text'] for term in terms), row['sequence']), reverse=True)
        kept, used = [], 0
        for row in rows:
            if len(kept) >= limit or used >= max_chars:
                break
            text = row['text'][:max_chars - used]
            kept.append({**row, 'text': text, 'excerpted': text != row['text']})
            used += len(text)
        return tuple(kept)
