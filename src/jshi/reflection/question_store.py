"""Discovery inbox, scan watermarks and leased six-question jobs in the reflection database."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, replace
from datetime import datetime
from uuid import uuid4

from jshi.core.params import introspection_max_queue, introspection_scene_max_segments
from .runner import internal_result_id
from .port import IntrospectionLevel, IntrospectionRequest, IntrospectionTrigger, IntrospectionUrgency, urgency_rank


def _json(value):
    def encode(item):
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError(f"unsupported discovery data: {type(item).__name__}")

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=encode)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _load_request(payload):
    value = json.loads(payload)
    for key in ('entry_at', 'entry_at_also', 'created_at'):
        if value.get(key) is not None:
            value[key] = datetime.fromisoformat(value[key])
    value['trigger'] = IntrospectionTrigger(value['trigger'])
    value['level'] = IntrospectionLevel(value['level'])
    value['urgency'] = IntrospectionUrgency(value['urgency'])
    return IntrospectionRequest(**value)


def _question_key(request):
    return _digest([request.subject_id, request.object_id, request.theme])


def _versions(request):
    supplied = request.evidence.get('source_versions')
    if supplied:
        if isinstance(supplied, str):
            supplied = [supplied]
        return sorted({_json(version) for version in supplied})
    # Old detectors lack a version field. Queue timestamps and freshly generated
    # request IDs describe delivery, not a change in the underlying evidence.
    return [_json([request.entry_ref, request.entry_at, request.evidence])]


def _source_keys(request):
    """Exact observations shared by the two automatic routes, independent of their topics."""
    if request.trigger == IntrospectionTrigger.EPISTEMIC_REVISED:
        return (request.evidence.get('transition_ref') or f'{request.entry_ref}@{request.entry_at.isoformat()}',)
    if request.trigger == IntrospectionTrigger.IDLE and request.entry_ref.startswith('segment:'):
        return tuple('segment:' + ref for ref in request.evidence.get('review_segments', ())) or (request.entry_ref,)
    if request.trigger == IntrospectionTrigger.EVENT_RESULT:
        return tuple(dict.fromkeys(ref for evidence in (request.evidence, *request.evidence.get('merged', ()))
                                   for ref in evidence.get('source_versions', ()) if str(ref).startswith('tool:')))
    return ()


def _carry_sources(previous, request):
    evidence = dict(previous.evidence, **request.evidence)
    limit = max(2, introspection_scene_max_segments())
    for field in ('segment_ids', 'source_versions', 'tool_facts', 'recall_events'):
        old_items, new_items = previous.evidence.get(field, ()), request.evidence.get(field, ())
        if not (old_items or new_items):
            continue
        if isinstance(old_items, str):
            old_items = (old_items,)
        if isinstance(new_items, str):
            new_items = (new_items,)
        # Keep the original anchor plus the most recent bounded evidence window.
        # The full lineage remains accessible through the immutable earlier revisions.
        distinct = {_json(item): item for item in (*old_items, *new_items)}
        items = list(distinct.values())
        evidence[field] = items if len(items) <= limit else [items[0], *items[1-limit:]]
    return replace(request, evidence=evidence, entry_at_also=request.entry_at_also or previous.entry_at_also
                   or (previous.entry_at if request.entry_at != previous.entry_at else None))


def _now(value):
    return time.time() if value is None else value


class QuestionStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def initialize(db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS inner_signals (
                id INTEGER PRIMARY KEY, subject TEXT NOT NULL, shadow INTEGER NOT NULL,
                identity TEXT NOT NULL, request TEXT NOT NULL, UNIQUE(subject,shadow,identity));
            CREATE INDEX IF NOT EXISTS inner_signal_order ON inner_signals(subject,shadow,id);
            CREATE TABLE IF NOT EXISTS inner_scans (
                subject TEXT NOT NULL, shadow INTEGER NOT NULL, source TEXT NOT NULL,
                position INTEGER NOT NULL, coverage TEXT NOT NULL, PRIMARY KEY(subject,shadow,source,position));
            CREATE TABLE IF NOT EXISTS inner_shadow_candidates (
                subject TEXT NOT NULL, topic TEXT NOT NULL, versions TEXT NOT NULL,
                request TEXT NOT NULL, PRIMARY KEY(subject,topic,versions));
            CREATE TABLE IF NOT EXISTS inner_runs (
                job_id INTEGER PRIMARY KEY, subject TEXT NOT NULL, topic TEXT NOT NULL,
                revision INTEGER NOT NULL, urgency INTEGER NOT NULL, state TEXT NOT NULL,
                token TEXT, lease_until REAL, input TEXT, frozen TEXT,
                UNIQUE(subject,topic,revision));
            CREATE INDEX IF NOT EXISTS inner_claimable ON inner_runs(subject,state,urgency,job_id);
            CREATE TABLE IF NOT EXISTS inner_facts (
                subject TEXT NOT NULL, topic TEXT NOT NULL, fact TEXT NOT NULL, job_id INTEGER NOT NULL,
                PRIMARY KEY(subject,topic,fact));
            CREATE TABLE IF NOT EXISTS inner_requests (
                subject TEXT NOT NULL, request_id TEXT NOT NULL, job_id INTEGER NOT NULL,
                PRIMARY KEY(subject,request_id));
            CREATE TABLE IF NOT EXISTS inner_followups (
                subject TEXT NOT NULL, topic TEXT NOT NULL, proposal_job INTEGER NOT NULL,
                suggestion TEXT NOT NULL, stage TEXT NOT NULL, segment TEXT NOT NULL,
                occurred_at TEXT NOT NULL, excerpt TEXT NOT NULL,
                PRIMARY KEY(subject,topic,proposal_job,suggestion,stage,segment));
            CREATE TABLE IF NOT EXISTS inner_source_owners (
                subject TEXT NOT NULL, source TEXT NOT NULL, owner TEXT NOT NULL, request_id TEXT NOT NULL,
                PRIMARY KEY(subject,source));
            CREATE TABLE IF NOT EXISTS inner_legacy_work (
                subject TEXT NOT NULL, request_id TEXT NOT NULL, request TEXT NOT NULL, token TEXT, lease_until REAL,
                PRIMARY KEY(subject,request_id));
            CREATE TABLE IF NOT EXISTS inner_legacy_migrated (subject TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS inner_deferred_segments (
                subject TEXT NOT NULL, shadow INTEGER NOT NULL, source TEXT NOT NULL,
                segment_id TEXT NOT NULL, due REAL NOT NULL, PRIMARY KEY(subject,shadow,source,segment_id));
            CREATE INDEX IF NOT EXISTS inner_deferred_due ON inner_deferred_segments(subject,shadow,source,due);
        """)

    def import_legacy(self, subject, repository):
        reader = getattr(repository, 'completed_reflection_sources', None)
        if not callable(reader):
            return
        with self.store.connect() as db:
            if db.execute('SELECT 1 FROM inner_legacy_migrated WHERE subject=?', (subject,)).fetchone():
                return
        # Only a completed old review with a verifiable source can own it.
        # Pre-upgrade tool summaries lack the receipt identity and version.
        sources = reader(subject)
        with self.store.connect() as db:
            db.executemany("INSERT INTO inner_source_owners VALUES(?,?,'legacy','') ON CONFLICT(subject,source) "
                           "DO UPDATE SET owner='legacy',request_id='' WHERE inner_source_owners.owner='signal'",
                           ((subject, ref) for ref in sources))
            db.execute('INSERT OR IGNORE INTO inner_legacy_migrated VALUES(?)', (subject,))

    def covered(self, subject, keys, *, owner=None, connection=None):
        if not keys:
            return set()
        with self.store.connect(connection) as db:
            rows = db.execute('SELECT source FROM inner_source_owners WHERE subject=? '
                              'AND source IN (SELECT value FROM json_each(?)) AND (? IS NULL OR owner=?)',
                              (subject, _json(list(keys)), owner, owner))
            return {row[0] for row in rows}

    def _admit_new(self, db, request, *, signal=False):
        keys = _source_keys(request)
        if keys and set(keys) == self.covered(request.subject_id, keys, owner='legacy', connection=db):
            return False
        owner = 'signal' if signal else 'discovery'
        db.executemany("INSERT INTO inner_source_owners VALUES(?,?,?,?) ON CONFLICT(subject,source) "
                       "DO UPDATE SET owner=excluded.owner,request_id=excluded.request_id "
                       "WHERE inner_source_owners.owner='signal' AND excluded.owner='discovery'",
                       ((request.subject_id, key, owner, request.request_id) for key in keys))
        return True

    def enqueue_legacy(self, request, queue, *, already_queued=False):
        from .port import IntrospectionStatus
        from .queue import EnqueueResult
        keys = _source_keys(request)
        if not keys:
            if already_queued:
                return None
            result = queue.enqueue(request)
            if result.evicted:
                with self.store.connect() as db:
                    self._forget_legacy(db, result.evicted)
            return result
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM inner_legacy_work WHERE subject=? AND request_id=?',
                          (request.subject_id, request.request_id)).fetchone():
                return EnqueueResult(IntrospectionStatus.DROPPED, 'duplicate')
            known = self.covered(request.subject_id, keys, connection=db)
            if already_queued:
                known -= self.covered(request.subject_id, keys, owner='signal', connection=db)
            fresh = sorted(set(keys) - known)
            if not fresh:
                return EnqueueResult(IntrospectionStatus.DROPPED, 'source_already_owned')
            if request.evidence.get('tool_failures') and known:
                failures = [item for item in request.evidence['tool_failures'] if item.get('source_id') in fresh]
                request = replace(request, entry_text='；'.join(item['summary'] for item in failures),
                                  evidence=dict(request.evidence, tool_failures=failures, source_versions=fresh))
            # A second receipt from the same activity, or a later revision of the same
            # cognition, should not be blocked by the old theme's cooldown.
            request = replace(request, dedup_key=_digest([request.subject_id, request.theme, fresh]))
            result = EnqueueResult(IntrospectionStatus.QUEUED) if already_queued else queue.enqueue(request)
            if result.status != IntrospectionStatus.QUEUED:
                return result
            if result.evicted:
                self._forget_legacy(db, result.evicted)
            db.executemany("INSERT INTO inner_source_owners VALUES(?,?,'legacy',?) ON CONFLICT(subject,source) "
                           "DO UPDATE SET owner='legacy',request_id=excluded.request_id WHERE inner_source_owners.owner='signal'",
                           ((request.subject_id, key, request.request_id) for key in fresh))
            db.execute('INSERT INTO inner_legacy_work(subject,request_id,request) VALUES(?,?,?)',
                       (request.subject_id, request.request_id, _json(asdict(request))))
            return result

    @staticmethod
    def _forget_legacy(db, request):
        args = (request.subject_id, request.request_id)
        db.execute("DELETE FROM inner_source_owners WHERE owner='legacy' AND subject=? AND request_id=?", args)
        db.execute('DELETE FROM inner_legacy_work WHERE subject=? AND request_id=?', args)

    def legacy_pending(self, subject):
        with self.store.connect() as db:
            return db.execute('SELECT 1 FROM inner_legacy_work WHERE subject=? AND (token IS NULL OR lease_until<=?) '
                              'LIMIT 1', (subject, time.time())).fetchone() is not None

    def claim_legacy(self, subject, request=None):
        now = time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            ident = request.request_id if request is not None else None
            row = db.execute('SELECT * FROM inner_legacy_work WHERE (? IS NULL OR subject=?) '
                             'AND (? IS NULL OR request_id=?) AND (token IS NULL OR lease_until<=?) ORDER BY rowid LIMIT 1',
                             (subject, subject, ident, ident, now)).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            db.execute('UPDATE inner_legacy_work SET token=?,lease_until=? WHERE subject=? AND request_id=?',
                       (token, now + 300, row['subject'], row['request_id']))
            return _load_request(row['request']), token

    def finish_legacy(self, request, token, *, done):
        with self.store.connect() as db:
            sql = ('DELETE FROM inner_legacy_work' if done else 'UPDATE inner_legacy_work SET token=NULL,lease_until=NULL')
            db.execute(sql + ' WHERE subject=? AND request_id=? AND token=?',
                       (request.subject_id, request.request_id, token))

    def put_signals(self, subject, requests, *, shadow=False):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for request in requests:
                if request.subject_id != subject:
                    raise ValueError('signal belongs to another subject')
                if not shadow and not self._admit_new(db, request, signal=True):
                    continue
                identity = _digest([_question_key(request), _versions(request)])
                db.execute('INSERT OR IGNORE INTO inner_signals(subject,shadow,identity,request) VALUES(?,?,?,?)',
                           (subject, int(shadow), identity, _json(asdict(request))))

    def next_signals(self, subject, *, shadow=False, limit=8, after=None):
        with self.store.connect() as db:
            if after is None:
                after = self._cursor(db, subject, 'signals', shadow)
            rows = db.execute('SELECT id,request FROM inner_signals WHERE subject=? AND shadow=? AND id>? '
                              'ORDER BY id LIMIT ?', (subject, int(shadow), after, max(0, limit))).fetchall()
        return [_load_request(row['request']) for row in rows], rows[-1]['id'] if rows else after

    def has_signals(self, subject, *, shadow=False):
        with self.store.connect() as db:
            after = self._cursor(db, subject, 'signals', shadow)
            return db.execute('SELECT 1 FROM inner_signals WHERE subject=? AND shadow=? AND id>? LIMIT 1',
                              (subject, int(shadow), after)).fetchone() is not None

    @staticmethod
    def _cursor(db, subject, key, shadow):
        return db.execute('SELECT COALESCE(MAX(position),0) FROM inner_scans '
                          'WHERE subject=? AND source=? AND shadow=?', (subject, key, int(shadow))).fetchone()[0]

    def cursor(self, subject, key, shadow=False):
        with self.store.connect() as db:
            return self._cursor(db, subject, key, shadow)

    def due_segments(self, subject, key, *, shadow=False, now, limit=8):
        with self.store.connect() as db:
            rows = db.execute('SELECT segment_id FROM inner_deferred_segments WHERE subject=? AND shadow=? '
                              'AND source=? AND due<=? ORDER BY due,segment_id LIMIT ?',
                              (subject, int(shadow), key, now, limit))
            return tuple(row[0] for row in rows)

    def submit(self, subject, requests, *, shadow=False, cursor_key=None, cursor=None,
               expected_cursor=None, coverage=(), accepted=None, deferred=(), resolved=()):
        admitted = []
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if cursor_key is not None:
                current = self._cursor(db, subject, cursor_key, shadow)
                if not isinstance(cursor, int) or cursor < current:
                    return False
                if expected_cursor is not None and expected_cursor != current:
                    return False
                scope = (subject, int(shadow), cursor_key)
                db.executemany('INSERT INTO inner_deferred_segments VALUES(?,?,?,?,?) '
                               'ON CONFLICT(subject,shadow,source,segment_id) DO UPDATE SET due=excluded.due',
                               ((*scope, ref, due) for ref, due in deferred))
                resolved_count = db.executemany('DELETE FROM inner_deferred_segments WHERE subject=? AND shadow=? '
                                                'AND source=? AND segment_id=?',
                                                ((*scope, ref) for ref in resolved)).rowcount
            for request in requests:
                job_id = self.submit_one(db, subject, request, shadow=shadow)
                if shadow or job_id is not None:
                    admitted.append(request)
            if cursor_key is not None:
                inserted = db.execute('INSERT OR IGNORE INTO inner_scans(subject,shadow,source,position,coverage) '
                                      'VALUES(?,?,?,?,?)', (*scope, cursor, _json(coverage))).rowcount
                if not inserted and resolved_count:
                    old = db.execute('SELECT coverage FROM inner_scans WHERE subject=? AND shadow=? AND source=? '
                                     'AND position=?', (*scope, cursor)).fetchone()
                    merged = {_json(item): item for item in (*json.loads(old['coverage']), *coverage)}
                    db.execute('UPDATE inner_scans SET coverage=? WHERE subject=? AND shadow=? AND source=? '
                               'AND position=?', (_json(list(merged.values())), *scope, cursor))
        if accepted is not None:
            accepted.extend(admitted)
        return True

    def submit_one(self, db, subject, request, *, shadow=False):
        if request.subject_id != subject:
            raise ValueError('question belongs to another subject')
        topic = _question_key(request)
        facts = _versions(request)
        payload = _json(asdict(request))
        if shadow:
            db.execute('INSERT OR IGNORE INTO inner_shadow_candidates VALUES(?,?,?,?)',
                       (subject, topic, _json(facts), payload))
            return
        if not self._admit_new(db, request):
            return
        previous = db.execute('SELECT job_id,revision,r.frozen,j.payload FROM inner_runs r JOIN thought_jobs j ON j.id=r.job_id '
                              'WHERE r.subject=? AND r.topic=? '
                              'ORDER BY revision DESC LIMIT 1', (subject, topic)).fetchone()
        new_facts = [fact for fact in facts if db.execute(
            'SELECT 1 FROM inner_facts WHERE subject=? AND topic=? AND fact=?',
            (subject, topic, fact)).fetchone() is None]
        if new_facts:
            revision = previous['revision'] + 1 if previous else 1
            if previous:
                carried = _carry_sources(_load_request(previous['payload']), request)
                if previous['frozen']:
                    answer = json.loads(previous['frozen']).get('answer', {})
                    evidence = dict(carried.evidence, previous_open_question=answer.get('open_question', ''),
                                    previous_waiting_for=answer.get('waiting_for', ''))
                    carried = replace(carried, evidence=evidence)
                payload = _json(asdict(carried))
                # A frozen answer is already accepted and may be halfway through
                # its cross-database delivery. Only invalidate uncommitted work.
                changed = db.execute("UPDATE inner_runs SET state='superseded',token=NULL,lease_until=NULL "
                                     "WHERE job_id=? AND state IN ('pending','backlog') AND frozen IS NULL",
                                     (previous['job_id'],)).rowcount
                if changed:
                    db.execute('UPDATE thought_jobs SET done=1 WHERE id=?', (previous['job_id'],))
            job_id = db.execute("INSERT INTO thought_jobs(subject,mode,payload,theme,created) "
                                "VALUES(?,'six_questions',?,?,?)", (subject, payload, topic, time.time())).lastrowid
            db.execute("INSERT INTO inner_runs(job_id,subject,topic,revision,urgency,state) "
                       "VALUES(?,?,?,?,?,'backlog')", (job_id, subject, topic, revision, urgency_rank(request.urgency)))
            db.executemany('INSERT INTO inner_facts VALUES(?,?,?,?)',
                           ((subject, topic, fact, job_id) for fact in new_facts))
        else:
            job_id = previous['job_id']
        db.execute('INSERT OR IGNORE INTO inner_requests VALUES(?,?,?)', (subject, request.request_id, job_id))
        self._fill_slots(db, subject)
        return job_id

    def lookup(self, subject, request_id):
        with self.store.connect() as db:
            row = db.execute('SELECT j.payload FROM inner_requests i JOIN inner_runs origin ON origin.job_id=i.job_id '
                             'JOIN inner_runs latest ON latest.subject=origin.subject AND latest.topic=origin.topic '
                             'JOIN thought_jobs j ON j.id=latest.job_id WHERE i.subject=? AND i.request_id=? '
                             'ORDER BY latest.revision DESC LIMIT 1', (subject, request_id)).fetchone()
        return _load_request(row['payload']) if row else None

    def suggestion_source(self, subject, request_id, *, connection=None):
        """Pin a suggestion to the completed revision the caller actually cited."""
        with self.store.connect(connection) as db:
            row = db.execute('SELECT r.* FROM inner_requests i JOIN inner_runs r ON r.job_id=i.job_id '
                             'WHERE i.subject=? AND i.request_id=?', (subject, request_id)).fetchone()
        if row is None:
            raise ValueError("当前主体没有这道内在认知题")
        suggestions = {}
        if row['frozen'] and row['state'] in ('done', 'waiting'):
            answer = json.loads(row['frozen']).get('answer', {})
            if answer.get('mode') == 'model' and not answer.get('degraded'):
                next_time = (answer.get('next_time') or '').strip()
                if next_time not in ('', '无', '否', '（未评）', '（材料不足）'):
                    suggestions['next_time'] = next_time
                if answer.get('tool_need') and answer.get('tool_capability'):
                    suggestions['tool'] = {key: answer.get(key, '') for key in
                                           ('tool_capability', 'tool_when', 'tool_worth')}
                interaction = answer.get('interaction_need')
                if interaction and interaction.get('object_id') and interaction.get('gist'):
                    suggestions['interaction'] = interaction
        job_key = f"inner:{row['topic']}:{row['revision']}"
        return dict(topic=row['topic'], job=row['job_id'], revision=row['revision'], suggestions=suggestions,
                    cognition_id=internal_result_id('cognition', subject, job_key))

    @staticmethod
    def select_suggestion(source, stage, suggestion):
        if stage == 'evidence':
            if suggestion:
                raise ValueError("普通补证不指定建议")
            return ''
        if stage not in ('adopted', 'outcome'):
            raise ValueError("无法识别的补证阶段")
        available = source['suggestions']
        if suggestion is None and len(available) == 1:
            return next(iter(available))
        if suggestion not in available:
            raise ValueError("请指定原题已经提出的建议，可用建议：" + ('、'.join(available) or '无'))
        return suggestion

    def followups(self, subject, request_id):
        with self.store.connect() as db:
            source = self.suggestion_source(subject, request_id, connection=db)
            rows = db.execute('SELECT r.revision AS suggestion_revision,f.suggestion,f.stage,'
                              'f.segment,f.occurred_at,f.excerpt FROM inner_followups f LEFT JOIN inner_runs r '
                              'ON r.job_id=f.proposal_job WHERE f.subject=? AND f.topic=? ORDER BY f.rowid',
                              (subject, source['topic']))
            observations = []
            for row in rows:
                record = dict(row)
                if row['suggestion_revision'] is not None:
                    key = f"inner:{source['topic']}:{row['suggestion_revision']}"
                    record['suggestion_cognition_id'] = internal_result_id('cognition', subject, key)
                observations.append(record)
            return {'revision': source['revision'], 'suggestions': source['suggestions'], 'observations': observations}

    def attach_followup(self, subject, source_id, request, segment, *, stage, suggestion):
        """A stage is an annotation of real evidence, never a new fact version by itself."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            source = self.suggestion_source(subject, source_id, connection=db)
            suggestion = self.select_suggestion(source, stage, suggestion)
            proposal_job = source['job'] if suggestion else 0
            key = subject, source['topic'], proposal_job, suggestion, stage, segment.segment_id
            if db.execute('SELECT 1 FROM inner_followups WHERE subject=? AND topic=? AND proposal_job=? '
                          'AND suggestion=? AND stage=? AND segment=?', key).fetchone():
                return
            job_id = self.submit_one(db, subject, request)
            if job_id is None:
                raise ValueError("这段材料无法交回原题")
            db.execute('INSERT INTO inner_followups VALUES(?,?,?,?,?,?,?,?)',
                       (*key, segment.occurred_at.isoformat(), segment.text_raw[:500]))

    @staticmethod
    def _fill_slots(db, subject):
        ready = db.execute("SELECT COUNT(*) FROM inner_runs WHERE subject=? AND state='pending'", (subject,)).fetchone()[0]
        slots = introspection_max_queue() - ready
        if slots > 0:
            db.execute("UPDATE inner_runs SET state='pending' WHERE job_id IN (SELECT job_id FROM inner_runs "
                       "WHERE subject=? AND state='backlog' ORDER BY job_id LIMIT ?)", (subject, slots))

    def has_pending(self, subject, *, now=None):
        with self.store.connect() as db:
            return db.execute("SELECT 1 FROM inner_runs WHERE subject=? AND state IN ('pending','backlog') "
                              'AND (token IS NULL OR lease_until<=?) LIMIT 1',
                              (subject, _now(now))).fetchone() is not None

    def next_subject(self):
        with self.store.connect() as db:
            row = db.execute("SELECT subject FROM inner_runs WHERE state='pending' "
                             "AND (token IS NULL OR lease_until<=?) ORDER BY urgency DESC,job_id LIMIT 1",
                             (time.time(),)).fetchone()
        return row['subject'] if row else None

    def submit_and_claim(self, request, *, now=None, lease_seconds=300):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if request.trigger == IntrospectionTrigger.MANUAL:
                request = replace(request, dedup_key='manual:' + request.request_id)
            self.submit_one(db, request.subject_id, request)
            return self.claim(request.subject_id, request_id=request.request_id, now=now,
                              lease_seconds=lease_seconds, connection=db)

    def claim(self, subject, *, request_id=None, now=None, lease_seconds=300, connection=None):
        now = _now(now)
        if lease_seconds <= 0:
            raise ValueError('positive lease required')
        with self.store.connect(connection) as db:
            if connection is None:
                db.execute('BEGIN IMMEDIATE')
            self._fill_slots(db, subject)
            restriction = ''
            states = "r.state='pending'"
            params = [subject, now]
            if request_id is not None:
                # Explicit synchronous requests need not wait for an idle window.
                states = "r.state IN ('pending','backlog')"
                restriction = ' AND r.job_id=(SELECT job_id FROM inner_requests WHERE subject=? AND request_id=?)'
                params.extend([subject, request_id])
            row = db.execute('SELECT r.*,j.payload FROM inner_runs r JOIN thought_jobs j ON j.id=r.job_id '
                             f"WHERE r.subject=? AND {states} AND (r.token IS NULL OR r.lease_until<=?)"
                             + restriction + ' ORDER BY (r.frozen IS NOT NULL) DESC, r.urgency DESC,r.job_id LIMIT 1',
                             params).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            db.execute("UPDATE inner_runs SET state='pending',token=?,lease_until=? WHERE job_id=?",
                       (token, now+lease_seconds, row['job_id']))
            return {'id': row['job_id'], 'job_key': f"inner:{row['topic']}:{row['revision']}",
                    'subject': subject, 'revision': row['revision'], 'token': token, 'lease_seconds': lease_seconds,
                    'request': _load_request(row['payload']), 'input': json.loads(row['input']) if row['input'] else None,
                    'frozen': json.loads(row['frozen']) if row['frozen'] else None}

    def _freeze(self, job, field, data, now):
        now = _now(now)
        with self.store.connect() as db:
            return bool(db.execute(f"UPDATE inner_runs SET {field}=?,lease_until=? WHERE subject=? AND job_id=? "
                                   f"AND token=? AND lease_until>? AND state='pending' AND {field} IS NULL",
                                   (_json(data), now+job['lease_seconds'], job['subject'], job['id'],
                                    job['token'], now)).rowcount)

    def freeze_input(self, job, data, *, now=None):
        return self._freeze(job, 'input', data, now)

    def freeze(self, job, data, *, now=None):
        return self._freeze(job, 'frozen', data, now)

    def release(self, job):
        with self.store.connect() as db:
            return bool(db.execute("UPDATE inner_runs SET token=NULL,lease_until=NULL WHERE job_id=? "
                                   "AND subject=? AND token=? AND state='pending'",
                                   (job['id'], job['subject'], job['token'])).rowcount)

    def complete(self, job, *, state='done', proposals=(), activity_id='', cognitive_content_id='', now=None):
        if state in ('pending', 'backlog', 'superseded') or not state:
            raise ValueError('invalid completion state')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            valid = db.execute("SELECT 1 FROM inner_runs WHERE job_id=? AND subject=? AND token=? "
                               "AND lease_until>? AND state='pending'", (job['id'],job['subject'],job['token'],
                                                                        _now(now))).fetchone()
            if not valid:
                return False
            if proposals:
                if not activity_id:
                    raise ValueError('review requires a stable originating activity')
                review = self.store.enqueue_thought(job['subject'], 'review_behavior_result',
                                                    {'proposals': proposals}, activity_id, connection=db)
                if review is None:
                    db.execute('UPDATE inner_runs SET token=NULL,lease_until=NULL WHERE job_id=?', (job['id'],))
                    return False
            db.execute('UPDATE inner_runs SET state=?,token=NULL,lease_until=NULL WHERE job_id=?',
                       (state, job['id']))
            db.execute('UPDATE thought_jobs SET done=1 WHERE id=?', (job['id'],))
            if cognitive_content_id:
                db.execute('INSERT OR IGNORE INTO inner_requests VALUES(?,?,?)',
                           (job['subject'], cognitive_content_id, job['id']))
            self._fill_slots(db, job['subject'])
            return True
