"""Multi-reference scores and bounded, isolated conversation candidates."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from statistics import stdev
from time import time

MAX_REFERENCES = 10


def unit(vector):
    if vector is None or len(vector) == 0 or not all(math.isfinite(x) for x in vector):
        return None
    norm = math.sqrt(sum(x*x for x in vector))
    return [x/norm for x in vector] if norm else None


def similarity(a, b):
    a, b = unit(a), unit(b)
    return sum(x*y for x, y in zip(a, b)) if a and b and len(a) == len(b) else -1.0


def references(entry):
    rows = entry.get('references')
    if not rows:
        rows = [{'embedding': v, 'quality': 1.0, 'basis': 'legacy_template'}
                for v in entry.get('templates', ())]
        rows = rows or [{'embedding': entry.get('embedding'), 'quality': 1.0, 'basis': 'legacy'}]
    kept = []
    for row in rows:
        vector = unit(row.get('embedding'))
        quality = row.get('quality', 1.0)
        if not vector or not isinstance(quality, (float, int)) or not math.isfinite(quality) or quality <= 0:
            continue
        if row.get('input_id') and any(row['input_id'] == old.get('input_id') for old in kept):
            continue
        if row.get('input_id') and any(row['input_id'] == old.get('input_id') for old in kept):
            continue
        if any(similarity(vector, old['embedding']) >= .995 for old in kept):
            continue
        kept.append({**row, 'embedding': vector, 'quality': min(1.0, quality)})
        if len(kept) == MAX_REFERENCES:
            break
    return kept


def score(vector, entry):
    rows = [row for row in references(entry) if len(row['embedding']) == len(vector)]
    values = [similarity(vector, row['embedding']) for row in rows]
    weight = sum(row['quality'] for row in rows)
    mean = sum(value*row['quality'] for value, row in zip(values, rows))/weight if weight else -1.0
    return {'score': mean, 'reference_count': len(rows),
            'stddev': stdev(values) if len(values) > 1 else None,
            'sample_limited': len(rows) < 3,
            'score_basis': 'quality_weighted_mean', 'calibrated_probability': False}


class VoiceCandidates:
    """No audio duplication or named-person memory writes. Feature-only archive."""
    def __init__(self, path: Path, *, clock=time, max_bytes=16*1024*1024,
                 max_actors=32, per_actor=20, max_age=7*86400):
        self.path, self.clock = path, clock
        self.max_bytes, self.max_actors, self.per_actor, self.max_age = max_bytes, max_actors, per_actor, max_age
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS candidates (input_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, session_id TEXT NOT NULL, embedding TEXT NOT NULL, quality REAL NOT NULL, at REAL NOT NULL, start_ms INTEGER, end_ms INTEGER, verified_object TEXT, promoted INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE INDEX IF NOT EXISTS candidate_actor ON candidates(actor_id)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def collect(self, input_id, actor_id, session_id, vector, seconds, *, overlap=False, start_ms=None, end_ms=None):
        vector = unit(vector)
        if overlap or seconds < 1.5 or not vector or not input_id or not actor_id:
            return 'ineligible'
        encoded = json.dumps(vector)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM candidates WHERE input_id=?', (input_id,)).fetchone():
                return 'duplicate'
            # Only this newly introduced candidate cache expires; original audio,
            # unknown history and formal references are never removed here.
            db.execute('DELETE FROM candidates WHERE at<?', (self.clock()-self.max_age,))
            actors = {row[0] for row in db.execute('SELECT DISTINCT actor_id FROM candidates')}
            if actor_id not in actors and len(actors) >= self.max_actors:
                return 'actor_limit'
            if db.execute('SELECT COUNT(*) FROM candidates WHERE actor_id=?', (actor_id,)).fetchone()[0] >= self.per_actor:
                return 'sample_limit'
            if start_ms is not None and end_ms is not None and db.execute(
                'SELECT 1 FROM candidates WHERE session_id=? AND start_ms<? AND end_ms>?',
                (session_id, end_ms, start_ms)).fetchone():
                return 'overlap'
            size = db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]
            if size+len(encoded.encode())+8192 > self.max_bytes:
                return 'capacity_limit'
            db.execute('INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,NULL,0)',
                       (input_id, actor_id, session_id, encoded, min(seconds/6, 1), self.clock(), start_ms, end_ms))
            return 'collected'

    def verify(self, input_id, object_id):
        with self.connect() as db:
            db.execute('UPDATE candidates SET verified_object=? WHERE input_id=?', (object_id, input_id))

    def verified(self, object_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute(
                'SELECT * FROM candidates WHERE verified_object=? AND promoted=0 AND at>=? ORDER BY at',
                (object_id, self.clock()-self.max_age))]
        return [{**row, 'embedding': json.loads(row['embedding'])} for row in rows]

    def mark_promoted(self, input_ids):
        with self.connect() as db:
            db.executemany('UPDATE candidates SET promoted=1 WHERE input_id=?', ((iid,) for iid in input_ids))
