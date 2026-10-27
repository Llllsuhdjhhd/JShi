"""Versioned, scoped recall cache. It never creates long-term memory events."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import time

from .port import RecalledFragment
from .selection import applicable, is_continuation, representation_version


def scope_key(request):
    # Unknown actor buckets never establish a person's scope.
    # Query-relative roles cannot be reused after the current speaker changes.
    # The new shape deliberately does not adopt old caches with unknown speaker.
    return json.dumps([request.session_id, sorted(request.object_ids),
                       request.interlocutor_object_id], ensure_ascii=False)


def encode(item):
    value = asdict(item)
    if item.occurred_at is not None:
        value['occurred_at'] = item.occurred_at.isoformat()
    return json.dumps(value, ensure_ascii=False, default=str)


def decode(raw):
    value = json.loads(raw)
    if value.get('occurred_at'):
        value['occurred_at'] = datetime.fromisoformat(value['occurred_at'])
    for key in ('source_ids', 'interlocutor_object_ids', 'visual_observations', 'supersedes'):
        if key in value:
            value[key] = tuple(value[key])
    return RecalledFragment(**{k: v for k, v in value.items() if k in RecalledFragment.__dataclass_fields__})


class RecallWorkingSetStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS recall_working_sets (
                    subject TEXT, scope TEXT, version INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL, PRIMARY KEY(subject,scope));
                CREATE TABLE IF NOT EXISTS recall_working_items (
                    subject TEXT, scope TEXT, event_id TEXT, representation TEXT,
                    payload TEXT, query TEXT, activity TEXT, updated REAL,
                    PRIMARY KEY(subject,scope,event_id));
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(recall_working_sets)')}
            if 'focus_activity' not in columns:
                db.execute("ALTER TABLE recall_working_sets ADD COLUMN focus_activity TEXT DEFAULT ''")
            if 'latest_run' not in columns:
                db.execute("ALTER TABLE recall_working_sets ADD COLUMN latest_run TEXT DEFAULT ''")

    def load_scope(self, request, *, ttl: float, similarity: float, reserve=False):
        if not request.object_ids:
            return 0, ()
        scope = scope_key(request)
        with self.store.connect() as db:
            if reserve:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT OR IGNORE INTO recall_working_sets(subject,scope,version,updated) VALUES(?,?,0,?)',
                           (request.subject_id, scope, time.time()))
                db.execute('UPDATE recall_working_sets SET latest_run=?,updated=? WHERE subject=? AND scope=?',
                           (request.run_id, time.time(), request.subject_id, scope))
            state = db.execute('SELECT version,focus_activity FROM recall_working_sets WHERE subject=? AND scope=?',
                               (request.subject_id, scope)).fetchone()
            rows = db.execute('SELECT * FROM recall_working_items WHERE subject=? AND scope=? AND updated>=? ORDER BY updated DESC,event_id',
                              (request.subject_id, scope, time.time() - ttl)).fetchall()
        result = []
        for row in rows:
            if is_continuation(request.query_text) and (not state or row['activity'] != state['focus_activity']):
                continue
            if applicable(request.query_text, row['query'], '', similarity):
                result.append(decode(row['payload']))
        return (int(state['version']) if state else 0), tuple(result)

    def commit_visible(self, request, items, *, expected_version: int, invalid_ids=(), max_items=24, ttl=1800):
        if not request.object_ids:
            return False
        scope, now = scope_key(request), time.time()
        if request.created_at and now - request.created_at > ttl:
            return False
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO recall_working_sets(subject,scope,version,updated) VALUES(?,?,0,?)',
                       (request.subject_id, scope, now))
            changed = db.execute("UPDATE recall_working_sets SET version=version+1,updated=?,focus_activity=? WHERE subject=? AND scope=? AND version=? AND (latest_run='' OR latest_run=?)",
                                 (now, request.activity_id, request.subject_id, scope, expected_version, request.run_id)).rowcount
            if not changed:
                return False
            for ident in invalid_ids:
                db.execute('DELETE FROM recall_working_items WHERE subject=? AND scope=? AND event_id=?',
                           (request.subject_id, scope, ident))
            for item in items:
                if item.event_id in invalid_ids:
                    continue
                db.execute('INSERT OR REPLACE INTO recall_working_items VALUES(?,?,?,?,?,?,?,?)',
                           (request.subject_id, scope, item.event_id, representation_version(item),
                            encode(item), request.query_text, request.activity_id, now))
            db.execute('DELETE FROM recall_working_items WHERE updated<?', (now - ttl,))
            db.execute('DELETE FROM recall_working_items WHERE subject=? AND scope=? AND event_id NOT IN '
                       '(SELECT event_id FROM recall_working_items WHERE subject=? AND scope=? ORDER BY updated DESC,event_id LIMIT ?)',
                       (request.subject_id, scope, request.subject_id, scope, max_items))
            # Inactive scopes are caches, not durable personal facts.
            db.execute('DELETE FROM recall_working_sets WHERE updated<?', (now - ttl,))
        return True
