"""自省的持久状态；压力样本、评价与实际可召回记忆分别存储。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from jshi.core.params import reflection_param as param
from jshi.textutil import query_terms


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def similarity(a, b):
    left, right = set(query_terms(a)), set(query_terms(b))
    return len(left & right) / max(1, len(left | right))


class ReflectionMemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA journal_mode=WAL')
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, subject TEXT, query TEXT, object_id TEXT,
                payload TEXT, created REAL, stage TEXT DEFAULT 'evaluate');
            CREATE INDEX IF NOT EXISTS observation_subject ON observations(subject,stage,id);
            CREATE TABLE IF NOT EXISTS feedback (
                subject TEXT, observation INTEGER, event_id TEXT, query TEXT,
                object_id TEXT, effect REAL, reason TEXT, created REAL,
                PRIMARY KEY(subject,observation,event_id));
            CREATE TABLE IF NOT EXISTS pressure (
                subject TEXT, id TEXT, bucket TEXT, payload TEXT, bytes INTEGER,
                rank INTEGER, created REAL, PRIMARY KEY(subject,id));
            CREATE TABLE IF NOT EXISTS standards (
                id INTEGER PRIMARY KEY, subject TEXT, text TEXT, evidence TEXT,
                snapshot TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS insights (
                subject TEXT, id TEXT, payload TEXT, bytes INTEGER, created REAL,
                PRIMARY KEY(subject,id));
            CREATE TABLE IF NOT EXISTS schedules (
                subject TEXT PRIMARY KEY, last_run REAL);
            CREATE TABLE IF NOT EXISTS pressure_state (subject TEXT PRIMARY KEY, phase TEXT);
            CREATE TABLE IF NOT EXISTS comparison_cursors (subject TEXT PRIMARY KEY, position INTEGER);
            CREATE TABLE IF NOT EXISTS thought_jobs (
                id INTEGER PRIMARY KEY, subject TEXT, mode TEXT, payload TEXT,
                theme TEXT, created REAL, done INTEGER DEFAULT 0);
            """)
            columns = {r['name'] for r in db.execute('PRAGMA table_info(observations)')}
            for name, default in (('candidate_ids', '[]'), ('outcome', '{}')):
                if name not in columns:
                    db.execute(f"ALTER TABLE observations ADD COLUMN {name} TEXT DEFAULT '{default}'")
            insight_columns = {r['name'] for r in db.execute('PRAGMA table_info(insights)')}
            if 'last_used' not in insight_columns:
                db.execute('ALTER TABLE insights ADD COLUMN last_used REAL DEFAULT 0')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=float(param('REFLECTION_DB_TIMEOUT_SECONDS')))
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def observe(self, subject, query, object_id, fragments):
        if not query.strip() or not fragments:
            return
        items = []
        for item in fragments[:int(param('REFLECTION_COMPARE_ITEMS')) * 4]:
            value = asdict(item)
            value['text'] = value['text'][:int(param('REFLECTION_ITEM_CHARS'))]
            value['content'] = value['content'][:int(param('REFLECTION_ITEM_CHARS'))]
            value['visual_observations'] = []
            items.append(value)
        with self.connect() as db:
            db.execute("INSERT INTO observations(subject,query,object_id,payload,created) VALUES(?,?,?,?,?)",
                       (subject, query, object_id or '', dump(items), time.time()))
            db.execute("DELETE FROM observations WHERE subject=? AND id NOT IN "
                       "(SELECT id FROM observations WHERE subject=? ORDER BY id DESC LIMIT ?)",
                       (subject, subject, int(param('REFLECTION_OBSERVATIONS'))))

    def pending(self, subject):
        with self.connect() as db:
            row = db.execute("SELECT * FROM observations WHERE subject=? AND stage!='done' ORDER BY id LIMIT 1",
                             (subject,)).fetchone()
        return dict(row) if row else None

    def link_outcome(self, subject, since, visible_ids, activity_id, plan):
        outcome = {'activity_id': activity_id, 'model_visible_ids': list(visible_ids),
                   'planned_response': plan, 'note': '回应计划不等于已经交付或解决问题'}
        with self.connect() as db:
            db.execute('UPDATE observations SET outcome=? WHERE subject=? AND created>=? AND stage=?',
                       (dump(outcome), subject, since, 'evaluate'))
            for ident in visible_ids:
                if ident.startswith('insight:'):
                    db.execute('UPDATE insights SET last_used=? WHERE subject=? AND id=?',
                               (time.time(), subject, ident.removeprefix('insight:')))

    def finish_stage(self, observation, stage):
        with self.connect() as db:
            db.execute('UPDATE observations SET stage=? WHERE id=?', (stage, observation))

    def set_candidates(self, observation, ids):
        with self.connect() as db:
            db.execute("UPDATE observations SET stage='compare',candidate_ids=? WHERE id=?", (dump(ids), observation))

    def save_feedback(self, observation, feedback, visible_ids):
        now = time.time()
        with self.connect() as db:
            for item in feedback:
                event_id = str(item.get('event_id', ''))
                effect = {'helpful': 1, 'irrelevant': -1, 'misleading': -1, 'neutral': 0}.get(item.get('judgment'))
                reason = str(item.get('reason', '')).strip()[:int(param('REFLECTION_ITEM_CHARS'))]
                if event_id not in visible_ids or effect is None or not reason:
                    continue
                db.execute('INSERT OR REPLACE INTO feedback VALUES(?,?,?,?,?,?,?,?)',
                           (observation['subject'], observation['id'], event_id, observation['query'],
                            observation['object_id'], effect, reason, now))
            db.execute('DELETE FROM feedback WHERE created<?',
                       (now - float(param('REFLECTION_FEEDBACK_DAYS')) * 86400,))
            db.execute('DELETE FROM feedback WHERE subject=? AND rowid NOT IN '
                       '(SELECT rowid FROM feedback WHERE subject=? ORDER BY created DESC LIMIT ?)',
                       (observation['subject'], observation['subject'], int(param('REFLECTION_FEEDBACK_ROWS'))))

    def adjustments(self, subject, query, object_id):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM feedback WHERE subject=? AND created>? ORDER BY created DESC LIMIT ?',
                              (subject, time.time() - float(param('REFLECTION_FEEDBACK_DAYS')) * 86400,
                               int(param('REFLECTION_FEEDBACK_ROWS')))).fetchall()
        signals = {}
        for row in rows:
            if row['object_id'] != (object_id or ''):
                continue
            match = similarity(query, row['query'])
            if match < float(param('REFLECTION_FEEDBACK_SIMILARITY')):
                continue
            age = (time.time() - row['created']) / (float(param('REFLECTION_FEEDBACK_DAYS')) * 86400)
            value = row['effect'] * match * max(0, 1 - age)
            signals.setdefault(row['event_id'], []).append(value)
        # 平均而非累加：重复召回不能无限强化主观评价。
        return {key: float(param('REFLECTION_FEEDBACK_WEIGHT')) * sum(values) / len(values)
                for key, values in signals.items()}

    def latest_standard(self, subject):
        with self.connect() as db:
            row = db.execute('SELECT * FROM standards WHERE subject=? ORDER BY id DESC LIMIT 1', (subject,)).fetchone()
        return dict(row) if row else None

    def samples(self, subject):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM pressure WHERE subject=? ORDER BY bucket,rank,created DESC', (subject,)).fetchall()
        return [dict(row, item=json.loads(row['payload'])) for row in rows]

    def add_candidates(self, subject, candidates, sources, object_id):
        added = []
        max_chars = int(param('REFLECTION_ITEM_CHARS'))
        with self.connect() as db:
            for candidate in candidates[:int(param('REFLECTION_COMPARE_ITEMS')) // 2]:
                text = str(candidate.get('content', '')).strip()
                all_refs = candidate.get('source_ids', [])
                if not isinstance(all_refs, list):
                    continue
                refs = tuple(dict.fromkeys(str(x) for x in all_refs))
                if not text or len(text) > max_chars or not refs or not set(refs) <= sources:
                    continue
                ident = hashlib.sha256(text.encode()).hexdigest()
                if db.execute('SELECT 1 FROM pressure WHERE subject=? AND id=?', (subject, ident)).fetchone():
                    continue
                # 先填 A，再填 B；后续交替更新，两个容器永不封闭。
                totals = {r['bucket']: r['n'] for r in db.execute(
                    'SELECT bucket,SUM(bytes) n FROM pressure WHERE subject=? GROUP BY bucket', (subject,))}
                state = db.execute('SELECT phase FROM pressure_state WHERE subject=?', (subject,)).fetchone()
                phase = state[0] if state else 'A'
                bucket = phase[-1]
                item = {'id': ident, 'content': text, 'source_ids': refs, 'object_id': object_id or '',
                        'relation': str(candidate.get('relation', ''))[:max_chars], 'kind': 'reflection_insight'}
                payload = dump(item)
                size = len(payload.encode())
                if size > int(param('REFLECTION_PRESSURE_BYTES')):
                    continue
                rank = db.execute('SELECT COALESCE(MAX(rank),0)+1 FROM pressure WHERE subject=? AND bucket=?',
                                  (subject, bucket)).fetchone()[0]
                db.execute('INSERT INTO pressure VALUES(?,?,?,?,?,?,?)',
                           (subject, ident, bucket, payload, size, rank, time.time()))
                if phase.startswith('rolling'):
                    phase = 'rollingB' if bucket == 'A' else 'rollingA'
                elif totals.get(bucket, 0) + size >= int(param('REFLECTION_PRESSURE_BYTES')):
                    phase = 'B' if bucket == 'A' else 'rollingA'
                db.execute('INSERT OR REPLACE INTO pressure_state VALUES(?,?)', (subject, phase))
                added.append(ident)
        return added

    def order_and_trim(self, subject, ordered_ids):
        """局部比较只改样本间的相对位置，未比较的样本不凭空打分。"""
        with self.connect() as db:
            for bucket in ('A', 'B'):
                rows = db.execute('SELECT id,rank FROM pressure WHERE subject=? AND bucket=? ORDER BY rank,created',
                                  (subject, bucket)).fetchall()
                ranks = {r['id']: r['rank'] for r in rows}
                ids = [ident for ident in ordered_ids if ident in ranks]
                positions = sorted(ranks[ident] for ident in ids)
                for ident, rank in zip(ids, positions):
                    db.execute('UPDATE pressure SET rank=? WHERE subject=? AND id=?', (rank, subject, ident))
                total = db.execute('SELECT COALESCE(SUM(bytes),0) FROM pressure WHERE subject=? AND bucket=?',
                                   (subject, bucket)).fetchone()[0]
                for row in db.execute('SELECT id,bytes FROM pressure WHERE subject=? AND bucket=? ORDER BY rank DESC,created',
                                      (subject, bucket)).fetchall():
                    if total <= int(param('REFLECTION_PRESSURE_BYTES')):
                        break
                    db.execute('DELETE FROM pressure WHERE subject=? AND id=?', (subject, row['id']))
                    total -= row['bytes']

    def standard_due(self, subject):
        rows = self.samples(subject)
        if not rows:
            return False
        previous = self.latest_standard(subject)
        if previous is None:
            return len(rows) >= 4
        baseline = set(json.loads(previous['snapshot']))
        # 只算仍在容器中、与上一版相比新增的不同认识；分母为当前实际占用。
        for bucket in ('A', 'B'):
            members = [r for r in rows if r['bucket'] == bucket]
            total = sum(r['bytes'] for r in members)
            new = sum(r['bytes'] for r in members if r['id'] not in baseline)
            if total and new / total >= float(param('REFLECTION_RENEW_RATIO')):
                return True
        return False

    def comparison_position(self, subject):
        with self.connect() as db:
            row = db.execute('SELECT position FROM comparison_cursors WHERE subject=?', (subject,)).fetchone()
        return row[0] if row else 0

    def advance_comparison(self, subject, count):
        with self.connect() as db:
            db.execute('INSERT INTO comparison_cursors VALUES(?,?) ON CONFLICT(subject) '
                       'DO UPDATE SET position=position+excluded.position', (subject, count))

    def save_standard(self, subject, text, evidence):
        snapshot = [r['id'] for r in self.samples(subject)]
        with self.connect() as db:
            db.execute('INSERT INTO standards(subject,text,evidence,snapshot,created) VALUES(?,?,?,?,?)',
                       (subject, text, dump(evidence), dump(snapshot), time.time()))

    def admit(self, subject, ids, base_bytes):
        """实际记忆独立于压力样本；淘汰压力样本不删除已沉淀记忆。"""
        ratio = float(param('REFLECTION_MEMORY_RATIO'))
        cap = min(int(param('REFLECTION_MEMORY_BYTES')),
                  max(int(param('REFLECTION_MEMORY_BOOTSTRAP_BYTES')), int(base_bytes * ratio / (1 - ratio))))
        stored = []
        with self.connect() as db:
            used = db.execute('SELECT COALESCE(SUM(bytes),0) FROM insights WHERE subject=?', (subject,)).fetchone()[0]
            for ident in ids:
                row = db.execute('SELECT * FROM pressure WHERE subject=? AND id=?', (subject, ident)).fetchone()
                if row is None or used + row['bytes'] > cap:
                    continue
                result = db.execute('INSERT OR IGNORE INTO insights(subject,id,payload,bytes,created) VALUES(?,?,?,?,?)',
                                    (subject, ident, row['payload'], row['bytes'], time.time()))
                if result.rowcount:
                    used += row['bytes']
                    stored.append(ident)
        return stored

    def recall_insights(self, subject, query, object_id, limit, allowed_objects=()):
        from jshi.memory.port import RecalledFragment
        with self.connect() as db:
            terms = list(query_terms(query))[:int(param('REFLECTION_COMPARE_ITEMS'))]
            if not terms or not limit:
                return []
            clauses = ' OR '.join("payload LIKE ? ESCAPE '\\'" for _ in terms)
            patterns = ['%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%' for term in terms]
            rows = db.execute('SELECT * FROM insights WHERE subject=? AND (' + clauses + ') '
                              'ORDER BY last_used DESC,created DESC LIMIT ?',
                              (subject, *patterns, int(param('REFLECTION_INSIGHT_CANDIDATES')))).fetchall()
        result = []
        for row in rows:
            item = json.loads(row['payload'])
            if object_id and item['object_id'] != object_id:
                continue
            if allowed_objects and item['object_id'] not in allowed_objects:
                continue
            score = similarity(query, item['content'])
            if score <= 0:
                continue
            age = max(0, time.time() - max(row['created'], row['last_used']))
            score *= 2 ** (-age / (float(param('REFLECTION_MEMORY_HALF_LIFE_DAYS')) * 86400))
            result.append(RecalledFragment(
                event_id='insight:' + row['id'], event_type='reflection_insight', kind='reflection_insight',
                text=item['content'], content='【自省形成的认识，可修订】' + item['content'],
                object_id=item['object_id'] or None, source_ids=tuple(item['source_ids']), score=score,
                occurred_at=datetime.fromtimestamp(row['created']).astimezone()))
        return sorted(result, key=lambda r: r.score, reverse=True)[:limit]

    def last_run(self, subject):
        with self.connect() as db:
            row = db.execute('SELECT last_run FROM schedules WHERE subject=?', (subject,)).fetchone()
        return row[0] if row else 0

    def enqueue_thought(self, subject, mode, payload, theme):
        with self.connect() as db:
            existing = db.execute('SELECT id FROM thought_jobs WHERE subject=? AND mode=? AND theme=? AND done=0',
                                  (subject, mode, theme)).fetchone()
            if existing:
                return existing[0]
            count = db.execute('SELECT COUNT(*) FROM thought_jobs WHERE subject=? AND done=0', (subject,)).fetchone()[0]
            from jshi.core.params import introspection_max_queue
            if count >= introspection_max_queue():
                return None
            return db.execute('INSERT INTO thought_jobs(subject,mode,payload,theme,created) VALUES(?,?,?,?,?)',
                              (subject, mode, dump(payload), theme, time.time())).lastrowid

    def pending_thought(self, subject):
        with self.connect() as db:
            row = db.execute('SELECT * FROM thought_jobs WHERE subject=? AND done=0 ORDER BY id LIMIT 1', (subject,)).fetchone()
        return dict(row) if row else None

    def finish_thought(self, ident):
        with self.connect() as db:
            db.execute('UPDATE thought_jobs SET done=1 WHERE id=?', (ident,))

    def ran(self, subject, now):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO schedules VALUES(?,?)', (subject, now))
