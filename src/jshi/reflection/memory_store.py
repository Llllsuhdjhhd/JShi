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
from uuid import uuid4

from jshi.core.params import reflection_param as param
from jshi.textutil import query_terms
from .question_store import QuestionStore


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def similarity(a, b):
    left, right = set(query_terms(a)), set(query_terms(b))
    return len(left & right) / max(1, len(left | right))


def evaluation_enabled(row):
    """Legacy observations remain eligible; shadow/disabled runs never feed back."""
    return (not row['run_id'] or
            (row['status'] not in {'disabled', 'empty_query'}
             and json.loads(row['details']).get('mode') != 1))


class ReflectionMemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.questions = QuestionStore(self)
        self.discovery_supported = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA journal_mode=WAL')
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, subject TEXT, query TEXT, object_id TEXT,
                payload TEXT, created REAL, stage TEXT DEFAULT 'evaluate');
            CREATE INDEX IF NOT EXISTS observation_subject ON observations(subject,stage,id);
            CREATE TABLE IF NOT EXISTS inner_rating_exports (
                observation INTEGER PRIMARY KEY, subject TEXT NOT NULL, token TEXT, lease_until REAL);
            CREATE INDEX IF NOT EXISTS inner_rating_subject ON inner_rating_exports(subject, observation);
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
            CREATE TABLE IF NOT EXISTS inner_route (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL, mode INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS pressure_state (subject TEXT PRIMARY KEY, phase TEXT);
            CREATE TABLE IF NOT EXISTS comparison_cursors (subject TEXT PRIMARY KEY, position INTEGER);
            CREATE TABLE IF NOT EXISTS thought_jobs (
                id INTEGER PRIMARY KEY, subject TEXT, mode TEXT, payload TEXT,
                theme TEXT, created REAL, done INTEGER DEFAULT 0);
            """)
            self.questions.initialize(db)
            db.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(observations)')}
            for name, default in (('candidate_ids', '[]'), ('outcome', '{}'), ('proposals', '[]')):
                if name not in columns:
                    db.execute(f"ALTER TABLE observations ADD COLUMN {name} TEXT DEFAULT '{default}'")
            for name, default in (('run_id', ''), ('activity_id', ''), ('status', 'legacy'),
                                  ('scope', '[]'), ('details', '{}'), ('evaluation', '{}'), ('deep_owner', 'legacy')):
                if name not in columns:
                    db.execute(f"ALTER TABLE observations ADD COLUMN {name} TEXT DEFAULT '{default}'")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS recall_run_unique ON observations(run_id) WHERE run_id!=''")
            db.execute('CREATE TABLE IF NOT EXISTS recall_schema (version INTEGER PRIMARY KEY)')
            db.execute('INSERT OR IGNORE INTO recall_schema VALUES(1)')
            feedback_columns = {r['name'] for r in db.execute('PRAGMA table_info(feedback)')}
            if 'scope' not in feedback_columns:
                db.execute("ALTER TABLE feedback ADD COLUMN scope TEXT DEFAULT ''")
            insight_columns = {r['name'] for r in db.execute('PRAGMA table_info(insights)')}
            if 'last_used' not in insight_columns:
                db.execute('ALTER TABLE insights ADD COLUMN last_used REAL DEFAULT 0')
            if 'outlets' not in insight_columns:
                db.execute("""ALTER TABLE insights ADD COLUMN outlets TEXT NOT NULL DEFAULT '["memory"]'""")
            db.execute('CREATE INDEX IF NOT EXISTS insights_recent ON insights(subject,created DESC,id)')

    @contextmanager
    def connect(self, existing=None):
        if existing is not None:
            yield existing
            return
        db = sqlite3.connect(self.path, timeout=float(param('REFLECTION_DB_TIMEOUT_SECONDS')))
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def discovery_mode(self, connection=None):
        """A newer on-disk overlay owns the routing watermark across process restarts."""
        from jshi.core.param_overlay import current_snapshot
        if not self.discovery_supported:
            return 0
        mode = int(param('INNER_ACTIVITY_MODE'))
        snapshot = current_snapshot()
        if snapshot is not None and snapshot.path is None:
            # Explicit test override has no persisted overlay epoch.
            return mode
        with self.connect(connection) as db:
            row = db.execute('SELECT version,mode FROM inner_route WHERE singleton=1').fetchone()
            if snapshot is None or (row and row['version'] >= snapshot.version):
                return row['mode'] if row else mode
            db.execute('INSERT INTO inner_route(singleton,version,mode) VALUES(1,?,?) '
                       'ON CONFLICT(singleton) DO UPDATE SET version=excluded.version,mode=excluded.mode '
                       'WHERE inner_route.version<excluded.version', (snapshot.version, mode))
            return db.execute('SELECT mode FROM inner_route WHERE singleton=1').fetchone()[0]

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
            pending_count = db.execute("SELECT count(*) FROM observations WHERE subject=? AND stage!='done'", (subject,)).fetchone()[0]
            if pending_count >= int(param('REFLECTION_OBSERVATIONS')):
                return
            db.execute("INSERT INTO observations(subject,query,object_id,payload,created) VALUES(?,?,?,?,?)",
                       (subject, query, object_id or '', dump(items), time.time()))
            db.execute("DELETE FROM observations WHERE subject=? AND stage='done' "
                       "AND id NOT IN (SELECT observation FROM inner_rating_exports) AND id NOT IN "
                       "(SELECT id FROM observations WHERE subject=? ORDER BY id DESC LIMIT ?)",
                       (subject, subject, int(param('REFLECTION_OBSERVATIONS'))))

    def observe_run(self, batch, visible):
        """The single authoritative record for new recall runs, including empty runs."""
        request = batch.request
        items = []
        for item in batch.candidates:
            value = asdict(item)
            # Keep the actual selected representation; raw originals remain at source.
            value['text'] = value['content'] = item.content or item.text
            value['visual_observations'] = []
            items.append(value)
        outcome = {'activity_id': request.activity_id,
                   'model_visible_ids': [item.event_id for item in visible],
                   'response_status': 'not_generated'}
        details = {'query_runs': batch.query_runs, 'skipped': batch.skipped,
                   'invalidated_ids': batch.invalidated_ids,
                   'candidate_ids': [item.event_id for item in batch.candidates],
                   'reused_ids': batch.reused_ids, 'elapsed_ms': batch.elapsed_ms,
                   'truncated': batch.truncated, 'input_revision': request.input_revision,
                   'policy_version': request.policy_version, 'session_id': request.session_id,
                   'mode': request.mode}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owner = 'discovery' if request.run_id and self.discovery_mode(db) == 2 else 'legacy'
            db.execute('INSERT OR IGNORE INTO observations(subject,query,object_id,payload,created,stage,run_id,activity_id,status,scope,details,outcome,deep_owner) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (request.subject_id, request.query_text, request.interlocutor_object_id or '',
                        dump(items), request.created_at or time.time(), 'waiting', request.run_id,
                        request.activity_id, batch.status, dump(request.object_ids), dump(details), dump(outcome), owner))
            # Bounded per subject; completed history is reclaimed before pending work.
            keep = int(param('REFLECTION_OBSERVATIONS'))
            db.execute("DELETE FROM observations WHERE subject=? AND stage='done' "
                       "AND id NOT IN (SELECT observation FROM inner_rating_exports) AND id NOT IN "
                       '(SELECT id FROM observations WHERE subject=? ORDER BY id DESC LIMIT ?)',
                       (request.subject_id, request.subject_id, keep))

    def link_run_outcome(self, run_id, plan, ratings):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM observations WHERE run_id=?', (run_id,)).fetchone()
            if row is None:
                return
            outcome = json.loads(row['outcome'])
            outcome.update(planned_response=plan, main_ratings=ratings, response_status='planned',
                           note='回应计划不等于交付；模型使用声明不等于真实有效')
            stage = 'evaluate' if evaluation_enabled(row) else 'done'
            pending_count = db.execute("SELECT count(*) FROM observations WHERE subject=? AND stage IN ('evaluate','reprocess','compare')",
                                       (row['subject'],)).fetchone()[0]
            if stage == 'evaluate' and pending_count >= int(param('REFLECTION_OBSERVATIONS')):
                stage = 'done'
                outcome['evaluation_skipped'] = 'queue_capacity'
            db.execute("UPDATE observations SET outcome=?,stage=CASE WHEN stage='waiting' THEN ? ELSE stage END "
                       "WHERE run_id=?", (dump(outcome), stage, run_id))
            for ident in outcome.get('model_visible_ids', ()):
                if ident.startswith('insight:'):
                    db.execute('UPDATE insights SET last_used=? WHERE subject=? AND id=?',
                               (time.time(), row['subject'], ident.removeprefix('insight:')))

    def fail_run(self, run_id, status):
        with self.connect() as db:
            row = db.execute('SELECT * FROM observations WHERE run_id=?', (run_id,)).fetchone()
            if row:
                outcome = json.loads(row['outcome'])
                outcome['response_status'] = status
                stage = 'evaluate' if evaluation_enabled(row) else 'done'
                db.execute("UPDATE observations SET outcome=?,stage=CASE WHEN stage='waiting' THEN ? ELSE stage END "
                           "WHERE run_id=?", (dump(outcome), stage, run_id))

    def link_delivery(self, activity_id, status):
        with self.connect() as db:
            for row in db.execute("SELECT id,outcome FROM observations WHERE activity_id=? AND run_id!=''", (activity_id,)).fetchall():
                outcome = json.loads(row['outcome'])
                outcome['delivery_status'] = status
                db.execute('UPDATE observations SET outcome=? WHERE id=?', (dump(outcome), row['id']))

    def note_working_result(self, run_id, committed):
        with self.connect() as db:
            row = db.execute('SELECT outcome FROM observations WHERE run_id=?', (run_id,)).fetchone()
            if row:
                outcome = json.loads(row['outcome'])
                outcome['working_set_committed'] = bool(committed)
                db.execute('UPDATE observations SET outcome=? WHERE run_id=?', (dump(outcome), run_id))

    def save_evaluation(self, observation, evaluation, reviewed_ids, connection=None):
        value = dict(evaluation, reviewed_ids=sorted(reviewed_ids), evaluator_version='recall-evaluation-v2',
                     evaluated_at=time.time())
        with self.connect(connection) as db:
            db.execute('UPDATE observations SET evaluation=? WHERE subject=? AND id=?',
                       (dump(value), observation['subject'], observation['id']))

    def finish_discovery_evaluation(self, task, answer, reviewed_ids, question):
        """Persist the feedback, six-question handoff and old observation close together."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            updated = db.execute("UPDATE observations SET stage='done' WHERE id=? AND subject=? AND stage='evaluate' "
                                 "AND run_id!='' AND deep_owner='discovery'",
                                 (task['id'], task['subject'])).rowcount
            if not updated:
                return False
            self.save_feedback(task, [r for r in answer['feedback'] if isinstance(r, dict)], reviewed_ids, connection=db)
            self.save_evaluation(task, answer, reviewed_ids, connection=db)
            if question is not None:
                self.questions.submit_one(db, task['subject'], question)
            db.execute('INSERT INTO inner_rating_exports(observation,subject) VALUES(?,?)',
                       (task['id'], task['subject']))
            return True

    def pending_projection(self, subject):
        with self.connect() as db:
            row = db.execute('SELECT observation AS id FROM inner_rating_exports WHERE subject=? AND '
                             '(token IS NULL OR lease_until<=?) ORDER BY observation LIMIT 1',
                             (subject, time.time())).fetchone()
        return dict(row) if row else None

    def claim_projection(self, subject, observation):
        token = uuid4().hex
        now = time.time()
        with self.connect() as db:
            taken = db.execute('UPDATE inner_rating_exports SET token=?,lease_until=? WHERE subject=? '
                               'AND observation=? AND (token IS NULL OR lease_until<=?)',
                               (token, now+300, subject, observation, now)).rowcount
            if not taken:
                return None
            row = db.execute('SELECT id,activity_id,outcome,evaluation FROM observations WHERE subject=? AND id=?',
                             (subject, observation)).fetchone()
            return dict(row, projection_token=token)

    def finish_projection(self, subject, task, *, success):
        with self.connect() as db:
            sql = ('DELETE FROM inner_rating_exports' if success else
                   'UPDATE inner_rating_exports SET token=NULL, lease_until=NULL')
            db.execute(sql + ' WHERE subject=? AND observation=? AND token=?',
                       (subject, task['id'], task['projection_token']))

    def pending(self, subject):
        with self.connect() as db:
            # Also retire shadow jobs queued by older versions, at any stage.
            for row in db.execute("SELECT * FROM observations WHERE subject=? AND stage!='done' AND run_id!=''",
                                  (subject,)).fetchall():
                if not evaluation_enabled(row):
                    db.execute("UPDATE observations SET stage='done' WHERE id=?", (row['id'],))
            # A crash/cancellation cannot strand a waiting run forever.
            db.execute("UPDATE observations SET stage='evaluate' WHERE subject=? AND stage='waiting' AND created<?",
                       (subject, time.time() - float(param('RECALL_OUTCOME_WAIT_SECONDS'))))
            row = db.execute("SELECT * FROM observations WHERE subject=? AND stage NOT IN ('done','waiting') "
                             "AND (? OR deep_owner!='discovery') ORDER BY id LIMIT 1",
                             (subject, int(self.discovery_supported))).fetchone()
        return dict(row) if row else None

    def link_outcome(self, subject, since, visible_ids, activity_id, plan):
        outcome = {'activity_id': activity_id, 'model_visible_ids': list(visible_ids),
                   'planned_response': plan, 'note': '回应计划不等于已经交付或解决问题'}
        with self.connect() as db:
            db.execute("UPDATE observations SET outcome=? WHERE subject=? AND created>=? AND stage=? AND run_id=''",
                       (dump(outcome), subject, since, 'evaluate'))
            for ident in visible_ids:
                if ident.startswith('insight:'):
                    db.execute('UPDATE insights SET last_used=? WHERE subject=? AND id=?',
                               (time.time(), subject, ident.removeprefix('insight:')))

    def finish_stage(self, observation, stage):
        with self.connect() as db:
            db.execute('UPDATE observations SET stage=? WHERE id=?', (stage, observation))

    def set_proposals(self, observation, items):
        with self.connect() as db:
            db.execute("UPDATE observations SET stage=?,proposals=?,candidate_ids=? WHERE id=? AND stage='reprocess'",
                       ('compare' if items else 'done', dump(items), dump([v['id'] for v in items]), observation))

    def save_feedback(self, observation, feedback, visible_ids, connection=None):
        now = time.time()
        with self.connect(connection) as db:
            for item in feedback:
                event_id = str(item.get('event_id', ''))
                effect = {'helpful': 1, 'irrelevant': -1, 'misleading': -1, 'neutral': 0}.get(item.get('judgment'))
                reason = str(item.get('reason', '')).strip()[:int(param('REFLECTION_ITEM_CHARS'))]
                if event_id not in visible_ids or effect is None or not reason:
                    continue
                db.execute('INSERT OR REPLACE INTO feedback(subject,observation,event_id,query,object_id,effect,reason,created,scope) VALUES(?,?,?,?,?,?,?,?,?)',
                           (observation['subject'], observation['id'], event_id, observation['query'],
                            observation['object_id'], effect, reason, now, observation.get('scope', '') if observation.get('run_id') else ''))
            db.execute('DELETE FROM feedback WHERE created<?',
                       (now - float(param('REFLECTION_FEEDBACK_DAYS')) * 86400,))
            db.execute('DELETE FROM feedback WHERE subject=? AND rowid NOT IN '
                       '(SELECT rowid FROM feedback WHERE subject=? ORDER BY created DESC LIMIT ?)',
                       (observation['subject'], observation['subject'], int(param('REFLECTION_FEEDBACK_ROWS'))))

    def adjustments(self, subject, query, object_id, *, object_ids=()):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM feedback WHERE subject=? AND created>? ORDER BY created DESC LIMIT ?',
                              (subject, time.time() - float(param('REFLECTION_FEEDBACK_DAYS')) * 86400,
                               int(param('REFLECTION_FEEDBACK_ROWS')))).fetchall()
        signals = {}
        for row in rows:
            if not param('RECALL_FEEDBACK_ENABLED'):
                break
            if row['object_id'] != (object_id or ''):
                continue
            if row['scope']:
                scope = set(json.loads(row['scope']))
                expected = set(object_ids) | ({object_id} if object_id else set())
                if not scope or scope != expected:
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

    def latest_standard(self, subject, connection=None):
        with self.connect(connection) as db:
            row = db.execute('SELECT * FROM standards WHERE subject=? ORDER BY id DESC LIMIT 1', (subject,)).fetchone()
        return dict(row) if row else None

    def samples(self, subject, connection=None):
        with self.connect(connection) as db:
            rows = db.execute('SELECT * FROM pressure WHERE subject=? ORDER BY bucket,rank,created DESC', (subject,)).fetchall()
        return [dict(row, item=json.loads(row['payload'])) for row in rows]

    def prepare_candidates(self, candidates, sources, object_id, subject=None, *, evidence=None):
        items = {}
        max_chars = int(param('REFLECTION_ITEM_CHARS'))
        for candidate in candidates[:int(param('REFLECTION_COMPARE_ITEMS')) // 2]:
            text = candidate.get('content')
            all_refs = candidate.get('source_ids')
            if not isinstance(text, str) or not text.strip() or len(text) > max_chars or not isinstance(all_refs, list):
                continue
            if not all(isinstance(ref, str) and ref in sources for ref in all_refs) or not all_refs:
                continue
            text = text.strip()
            # 同一句认识可以分别针对不同人物，试验比较不能合并其范围。
            scope = candidate.get('person_scope')
            owner = object_id or scope or ''
            ident = hashlib.sha256(dump([owner, text]).encode()).hexdigest()
            item = {'id': ident, 'content': text, 'source_ids': list(dict.fromkeys(all_refs)),
                    'object_id': owner, 'relation': str(candidate.get('relation', ''))[:max_chars],
                    'kind': 'reflection_insight'}
            if scope and scope == owner:
                item['person_scope'] = scope
            if len(dump(item).encode()) <= int(param('REFLECTION_PRESSURE_BYTES')):
                if evidence:
                    remaining = max_chars
                    excerpts = {}
                    for ref in item['source_ids']:
                        snippet = str(evidence.get(ref) or '')[:remaining]
                        if snippet:
                            excerpts[ref] = snippet
                            remaining -= len(snippet)
                    item['evidence_excerpts'] = excerpts
                items[ident] = item
        if subject is not None and items:
            # 升级前主键只取正文哈希。同一人物的旧认识继续沿用原 ID。
            with self.connect() as db:
                for item in items.values():
                    old = hashlib.sha256(item['content'].encode()).hexdigest()
                    row = db.execute('SELECT payload FROM pressure WHERE subject=? AND id=? UNION ALL '
                                     'SELECT payload FROM insights WHERE subject=? AND id=? LIMIT 1',
                                     (subject, old, subject, old)).fetchone()
                    if row and json.loads(row[0]).get('object_id', '') == item['object_id']:
                        item['id'] = old
        return list(items.values())

    def add_candidates(self, subject, candidates, sources, object_id):
        items = self.prepare_candidates(candidates, sources, object_id, subject)
        with self.connect() as db:
            return self._insert_samples(subject, items, db)

    def _insert_samples(self, subject, items, db):
        added = []
        for item in items:
            ident = item['id']
            if db.execute('SELECT 1 FROM pressure WHERE subject=? AND id=?', (subject, ident)).fetchone():
                continue
            # 先填 A，再填 B；后续交替更新，两个容器永不封闭。
            totals = {r['bucket']: r['n'] for r in db.execute(
                'SELECT bucket,SUM(bytes) n FROM pressure WHERE subject=? GROUP BY bucket', (subject,))}
            state = db.execute('SELECT phase FROM pressure_state WHERE subject=?', (subject,)).fetchone()
            phase = state[0] if state else 'A'
            bucket = phase[-1]
            payload = dump({key: value for key, value in item.items() if key != 'evidence_excerpts'})
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

    def order_and_trim(self, subject, ordered_ids, connection=None):
        """局部比较只改样本间的相对位置，未比较的样本不凭空打分。"""
        with self.connect(connection) as db:
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

    def standard_due(self, subject, connection=None):
        with self.connect(connection) as db:
            rows = db.execute('SELECT id,bucket,bytes FROM pressure WHERE subject=?', (subject,)).fetchall()
        if not rows:
            return False
        previous = self.latest_standard(subject, connection)
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

    def advance_comparison(self, subject, count, connection=None):
        with self.connect(connection) as db:
            db.execute('INSERT INTO comparison_cursors VALUES(?,?) ON CONFLICT(subject) '
                       'DO UPDATE SET position=position+excluded.position', (subject, count))

    def save_standard(self, subject, text, evidence, connection=None):
        with self.connect(connection) as db:
            snapshot = [r[0] for r in db.execute('SELECT id FROM pressure WHERE subject=?', (subject,))]
            db.execute('INSERT INTO standards(subject,text,evidence,snapshot,created) VALUES(?,?,?,?,?)',
                       (subject, text, dump(evidence), dump(snapshot), time.time()))

    def admit(self, subject, ids, base_bytes, *, outlets=None, connection=None):
        """实际记忆独立于压力样本；淘汰压力样本不删除已沉淀记忆。"""
        ratio = float(param('REFLECTION_MEMORY_RATIO'))
        cap = min(int(param('REFLECTION_MEMORY_BYTES')),
                  max(int(param('REFLECTION_MEMORY_BOOTSTRAP_BYTES')), int(base_bytes * ratio / (1 - ratio))))
        stored = []
        with self.connect(connection) as db:
            used = db.execute('SELECT COALESCE(SUM(bytes),0) FROM insights WHERE subject=?', (subject,)).fetchone()[0]
            memory_used = db.execute("SELECT COALESCE(SUM(bytes),0) FROM insights WHERE subject=? AND EXISTS "
                                     "(SELECT 1 FROM json_each(outlets) WHERE value='memory')", (subject,)).fetchone()[0]
            for ident in ids:
                row = db.execute('SELECT * FROM pressure WHERE subject=? AND id=?', (subject, ident)).fetchone()
                if row is None:
                    continue
                kinds = list(outlets[ident]) if outlets is not None else ['memory']
                if 'person_experience' in kinds:
                    item = json.loads(row['payload'])
                    if not item.get('person_scope') or item['person_scope'] != item.get('object_id'):
                        kinds.remove('person_experience')
                existing = db.execute('SELECT outlets FROM insights WHERE subject=? AND id=?', (subject, ident)).fetchone()
                old = json.loads(existing[0]) if existing else []
                if 'memory' in kinds and 'memory' not in old and memory_used + row['bytes'] > cap:
                    kinds.remove('memory')
                if not kinds or (not existing and used + row['bytes'] > int(param('REFLECTION_MEMORY_BYTES'))):
                    continue
                merged = list(dict.fromkeys([*old, *kinds]))
                if merged == old:
                    continue
                if existing:
                    db.execute('UPDATE insights SET outlets=? WHERE subject=? AND id=?', (dump(merged), subject, ident))
                else:
                    db.execute('INSERT INTO insights(subject,id,payload,bytes,created,outlets) VALUES(?,?,?,?,?,?)',
                               (subject, ident, row['payload'], row['bytes'], time.time(), dump(kinds)))
                    used += row['bytes']
                if 'memory' in kinds and 'memory' not in old:
                    memory_used += row['bytes']
                stored.append(ident)
        return stored

    def commit_review(self, subject, task, proposals, decision, base_bytes, *, update_standard):
        """去留、试验区限额和任务进度一并提交。中断时从原任务重试。"""
        reviewed = set(decision.ordered)
        discarded = set(decision.discard)
        remaining = [item for item in proposals if item['id'] not in reviewed]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            observation, job = task.get('id'), task.get('job_id')
            if observation:
                row = db.execute("SELECT proposals FROM observations WHERE subject=? AND id=? AND stage='compare'",
                                 (subject, observation)).fetchone()
                if not row or json.loads(row[0]) != proposals:
                    return None
            if job:
                row = db.execute('SELECT payload FROM thought_jobs WHERE subject=? AND id=? AND done=0',
                                 (subject, job)).fetchone()
                if not row or json.loads(row[0])['proposals'] != proposals:
                    return None
            db.executemany('DELETE FROM pressure WHERE subject=? AND id=?', [(subject, key) for key in discarded])
            kept = reviewed - discarded
            self._insert_samples(subject, [item for item in proposals if item['id'] in kept], db)
            admitted = self.admit(subject, decision.admit, base_bytes, outlets=decision.outlets, connection=db)
            self.order_and_trim(subject, decision.ordered, db)
            self.advance_comparison(subject, max(1, len(reviewed) // 2), db)
            updated = update_standard and self.standard_due(subject, db)
            if updated:
                prev = self.latest_standard(subject, db)
                self.save_standard(subject, decision.standard, {
                    'ordered_ids': list(decision.ordered), 'reasons': decision.reasons,
                    'previous_version': prev['id'] if prev else None}, db)
            if observation:
                db.execute('UPDATE observations SET proposals=?,stage=? WHERE subject=? AND id=?',
                           (dump(remaining), 'compare' if remaining else 'done', subject, observation))
            if job:
                db.execute('UPDATE thought_jobs SET payload=?,done=? WHERE subject=? AND id=?',
                           (dump({'proposals': remaining}), int(not remaining), subject, job))
            delivered = {}
            for key in decision.admit:
                row = db.execute('SELECT outlets FROM insights WHERE subject=? AND id=?', (subject, key)).fetchone()
                delivered[key] = [kind for kind in decision.outlets[key] if row and kind in json.loads(row[0])]
        return admitted, updated, delivered

    def experiences(self, subject, outlet, *, object_id=None, limit=4):
        """已接纳的自身经验或指定人物的相处经验。试验样本与普通记忆不在此视图中。"""
        if outlet not in ('personal_experience', 'person_experience') or limit <= 0:
            return []
        if outlet == 'person_experience' and not object_id:
            return []
        with self.connect() as db:
            rows = db.execute('SELECT payload,created FROM insights WHERE subject=? AND EXISTS '
                              '(SELECT 1 FROM json_each(outlets) WHERE value=?) '
                              "AND (? IS NULL OR json_extract(payload,'$.object_id')=?) "
                              'ORDER BY created DESC,id LIMIT ?', (subject, outlet, object_id, object_id, limit))
            return [dict(json.loads(row['payload']), created=row['created']) for row in rows]

    def recall_insights(self, subject, query, object_id, limit, allowed_objects=()):
        from jshi.memory.port import RecalledFragment
        with self.connect() as db:
            terms = list(query_terms(query))[:int(param('REFLECTION_COMPARE_ITEMS'))]
            if not terms or not limit:
                return []
            clauses = ' OR '.join("payload LIKE ? ESCAPE '\\'" for _ in terms)
            patterns = ['%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%' for term in terms]
            rows = db.execute("SELECT * FROM insights WHERE subject=? AND EXISTS "
                              "(SELECT 1 FROM json_each(outlets) WHERE value='memory') AND (" + clauses + ') '
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

    def enqueue_thought(self, subject, mode, payload, theme, connection=None):
        with self.connect(connection) as db:
            if connection is None:
                db.execute('BEGIN IMMEDIATE')
            # Review themes are immutable originating activity IDs. A consumed review
            # is still a delivery receipt if a producer restarts before acknowledging it.
            existing = db.execute("SELECT id FROM thought_jobs WHERE subject=? AND mode=? AND theme=? "
                                  "AND (done=0 OR mode='review_behavior_result')", (subject, mode, theme)).fetchone()
            if existing:
                return existing[0]
            count = db.execute("SELECT COUNT(*) FROM thought_jobs WHERE subject=? AND done=0 AND mode!='six_questions'",
                               (subject,)).fetchone()[0]
            from jshi.core.params import introspection_max_queue
            if count >= introspection_max_queue():
                return None
            return db.execute('INSERT INTO thought_jobs(subject,mode,payload,theme,created) VALUES(?,?,?,?,?)',
                              (subject, mode, dump(payload), theme, time.time())).lastrowid

    def pending_thought(self, subject):
        with self.connect() as db:
            row = db.execute("SELECT * FROM thought_jobs WHERE subject=? AND done=0 AND mode!='six_questions' "
                             "ORDER BY id LIMIT 1", (subject,)).fetchone()
        return dict(row) if row else None

    def finish_thought(self, ident):
        with self.connect() as db:
            db.execute('UPDATE thought_jobs SET done=1 WHERE id=?', (ident,))

    def ran(self, subject, now):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO schedules VALUES(?,?)', (subject, now))
