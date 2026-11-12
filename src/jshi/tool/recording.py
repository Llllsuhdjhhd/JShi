"""Durable dialogue recording. Input events are written without model calls."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

COMMAND = 'runtime:dialogue-recording'
TOOL = {'name': COMMAND, 'description': '从宿主实际对话源记录原话到TXT；支持持续记录、导出近期原话、查看、停止和恢复。默认记录委托人与匠石双方，语音只记录有交付证据的回复。不会把任务说明或模型生成的摘要当成原话。',
        'params': {'type': 'object', 'required': ['action'], 'properties': {
            'action': {'enum': ['start', 'status', 'stop', 'resume', 'export']},
            'path': {'type': 'string', 'description': '目标TXT绝对路径；开始时可省略，默认桌面新文件'},
            'scope': {'enum': ['owner', 'all']},
            'include_replies': {'type': 'boolean', 'description': '默认true；只要人的原话时false'},
            'history': {'enum': ['request', 'available'], 'description': 'request从本次委托起；available包含宿主保留的近期原话，不代表全部历史'},
            'since': {'type': 'string', 'description': '可选起始时间ISO格式，必须含时区；不编造缺失原话'}}}}


class DialogueRecorder:
    def __init__(self, path, *, desktop=None, on_change=None):
        self.path = Path(path)
        self.desktop = Path(desktop) if desktop else Path.home() / 'Desktop'
        self.on_change = on_change
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT, owner TEXT, event_id TEXT, label TEXT, text TEXT, stamp TEXT,
                    UNIQUE(subject, event_id));
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, subject TEXT, owner TEXT,
                    work_id TEXT, path TEXT, scope TEXT, state TEXT, start_seq INTEGER,
                    stop_seq INTEGER, cursor INTEGER, prefix BLOB, content BLOB, error TEXT);
                CREATE TABLE IF NOT EXISTS job_works(job_id TEXT, work_id TEXT, PRIMARY KEY(job_id, work_id));
                CREATE TABLE IF NOT EXISTS cache_watermarks(subject TEXT, owner TEXT, removed_through INTEGER,
                    PRIMARY KEY(subject, owner));
            ''')
            for table, column, definition in (
                ('events', 'role', "TEXT NOT NULL DEFAULT 'user'"),
                ('events', 'basis', "TEXT NOT NULL DEFAULT ''"),
                ('jobs', 'include_replies', 'INTEGER NOT NULL DEFAULT 1'),
                ('jobs', 'since', "TEXT NOT NULL DEFAULT ''"),
                ('jobs', 'history', "TEXT NOT NULL DEFAULT 'request'"),
            ):
                if column not in {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def latest(self, subject, owner):
        with self._lock, self._db() as db:
            return db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE subject=? AND owner=?',
                              (subject, owner)).fetchone()[0]

    def owner_for(self, subject, event_id):
        with self._lock, self._db() as db:
            row = db.execute('SELECT owner FROM events WHERE subject=? AND event_id=? AND role="user"',
                             (subject, event_id)).fetchone()
            return row[0] if row else ''

    def recent_inputs(self, subject, owner):
        with self._lock, self._db() as db:
            rows = db.execute('SELECT text FROM events WHERE subject=? AND owner=? AND role="user" '
                              'ORDER BY seq DESC LIMIT 12', (subject, owner)).fetchall()
            return [r[0][:800] for r in reversed(rows)]

    def observe(self, subject, owner, event_id, text, label='', stamp='', *, role='user', basis='', control=True):
        if not all((subject, owner, event_id, text.strip())):
            return
        changes = []
        with self._lock, self._db() as db:
            previous = db.execute('SELECT seq,owner,label,text,role,basis FROM events WHERE subject=? AND event_id=?',
                                  (subject, event_id)).fetchone()
            if previous:
                if (previous['text'], previous['owner'], previous['label'], previous['role'], previous['basis']) == (text, owner, label or owner, role, basis):
                    return
                seq = previous['seq']
                # Identity corrections and ASR revisions replace the same source line.
                db.execute('UPDATE events SET text=?,label=?,owner=?,role=?,basis=? WHERE seq=?', (text, label or owner, owner, role, basis, seq))
            else:
                inserted = db.execute('INSERT INTO events(subject,owner,event_id,label,text,stamp,role,basis) VALUES(?,?,?,?,?,?,?,?)',
                    (subject, owner, event_id, label or owner, text, stamp or datetime.now(timezone.utc).isoformat(timespec='seconds'), role, basis))
                seq = inserted.lastrowid
            # Deliberately narrow control: quoted/negated/ambiguous "停" is ordinary content.
            stop = control and role == 'user' and re.fullmatch(r'(?:匠石[，,\s]*)?(?:请)?(?:停止记录|停止录制|结束记录|结束录制|不再记录|别再记录了)[。！!\s]*', text.strip())
            jobs = db.execute("SELECT * FROM jobs WHERE subject=? AND state IN ('running','blocked')", (subject,)).fetchall()
            for job in jobs:
                if job['owner'] != owner and job['scope'] != 'all' and (previous is None or job['owner'] != previous['owner']):
                    continue
                if stop and job['owner'] == owner:
                    db.execute('UPDATE jobs SET stop_seq=? WHERE id=?', (seq-1, job['id']))
                if job['state'] == 'running' or (stop and job['owner'] == owner):
                    updated = self._flush(db, job['id'])
                    if updated['state'] != job['state']:
                        changes.append(updated)
            # Keep a small recent window plus every event still owned by a recording.
            removed = db.execute('DELETE FROM events WHERE subject=? AND seq<? AND NOT EXISTS '
                       '(SELECT 1 FROM jobs WHERE jobs.subject=events.subject AND events.seq>=jobs.start_seq '
                       'AND (jobs.stop_seq IS NULL OR events.seq<=jobs.stop_seq)) RETURNING owner,seq', (subject, seq-200)).fetchall()
            for deleted in removed:
                db.execute('INSERT INTO cache_watermarks VALUES(?,?,?) ON CONFLICT(subject,owner) '
                    'DO UPDATE SET removed_through=MAX(removed_through,excluded.removed_through)',
                    (subject, deleted['owner'], deleted['seq']))
        for row in changes:
            if self.on_change:
                self.on_change(row)

    def _flush(self, db, job_id):
        row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        events = db.execute('SELECT * FROM events WHERE subject=? AND seq>=? AND (? IS NULL OR seq<=?) '
            'AND (?="all" OR owner=?) AND (? OR role="user") AND (?="" OR stamp>=?) ORDER BY stamp,seq',
            (row['subject'], row['start_seq'], row['stop_seq'], row['stop_seq'], row['scope'], row['owner'], row['include_replies'], row['since'], row['since'])).fetchall()
        content = bytes(row['prefix'])
        if content and not content.endswith(b'\n'):
            content += b'\n'
        content += ''.join(f"[{e['stamp']}] {e['label']}" + (f"（{e['basis']}）" if e['basis'] else '') + f"：{e['text']}\n" for e in events).encode('utf-8')
        target = Path(row['path'])
        temporary = None
        try:
            if len(content) > 8 * 1024 * 1024:
                raise ValueError('记录达到8MB上限，请停止后新建记录')
            actual = target.read_bytes() if target.exists() else b''
            # Also accept desired content: recovery after replace succeeded but DB commit did not.
            if actual not in (bytes(row['content']), content):
                raise ValueError('文件被外部改动，已暂停，未覆盖外部内容')
            if actual != content or not target.exists():
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.jshi-record-', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if (target.read_bytes() if target.exists() else b'') != actual:
                    raise ValueError('写入期间文件发生外部改动，已暂停')
                os.replace(temporary, target)
                temporary = None
            if target.read_bytes() != content:
                raise ValueError('回读内容不一致')
            state = 'stopped' if row['stop_seq'] is not None else 'running'
            cursor = max((e['seq'] for e in events), default=row['start_seq']-1)
            db.execute('UPDATE jobs SET content=?,cursor=?,state=?,error="" WHERE id=?',
                       (content, cursor, state, job_id))
        except (OSError, ValueError) as exc:
            db.execute('UPDATE jobs SET state="blocked",error=? WHERE id=?', (str(exc), job_id))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return self._public(db, job_id)

    def _public(self, db, job_id):
        row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        row['bytes'] = len(row['content'])
        row['sha256'] = hashlib.sha256(row['content']).hexdigest()
        removed = db.execute('SELECT MAX(removed_through) FROM cache_watermarks WHERE subject=? '
            'AND (?="all" OR owner=?)', (row['subject'], row['scope'], row['owner'])).fetchone()[0]
        row['input_gap'] = removed is not None and removed >= row['start_seq']
        events = db.execute('SELECT label,text,role FROM events WHERE subject=? AND seq>=? '
            'AND (? IS NULL OR seq<=?) AND (?="all" OR owner=?) AND (? OR role="user") '
            'AND (?="" OR stamp>=?) ORDER BY stamp,seq',
            (row['subject'], row['start_seq'], row['stop_seq'], row['stop_seq'], row['scope'], row['owner'],
             row['include_replies'], row['since'], row['since'])).fetchall()
        # Only report events whose rendered bytes were actually flushed and checked.
        users = [e for e in events if e['role'] == 'user'] if row['state'] != 'blocked' else []
        row['user_lines'] = len(users)
        row['reply_lines'] = sum(e['role'] == 'assistant' for e in events) if row['state'] != 'blocked' else 0
        sample = users if len(users) <= 8 else users[:6] + users[-2:]
        row['excerpt'] = [{'speaker': e['label'], 'text': e['text'][:120]} for e in sample]
        row.pop('content')
        row.pop('prefix')
        row['work_ids'] = list(dict.fromkeys([row['work_id'], *(r[0] for r in db.execute(
            'SELECT work_id FROM job_works WHERE job_id=?', (job_id,)).fetchall())]))
        return row

    def command(self, subject, owner, work_id, params, *, start_seq=None):
        unknown = set(params) - set(TOOL['params']['properties'])
        if unknown:
            raise ValueError('记录器只接受范围和操作参数，不接受模型提供的聊天正文：' + ','.join(sorted(unknown)))
        action = params.get('action')
        if action not in {'start', 'status', 'stop', 'resume', 'export'}:
            raise ValueError('未知记录操作')
        scope = params.get('scope', 'owner')
        if scope not in {'owner', 'all'}:
            raise ValueError('未知记录范围')
        include_replies = params.get('include_replies', True)
        if type(include_replies) is not bool:
            raise ValueError('是否记录回复须为布尔值')
        history = params.get('history', 'available' if action == 'export' else 'request')
        if history not in {'request', 'available'}:
            raise ValueError('未知历史记录范围')
        since = params.get('since', '')
        if since:
            value = datetime.fromisoformat(since)
            if value.tzinfo is None:
                raise ValueError('起始时间必须包含时区')
            since = value.astimezone(timezone.utc).isoformat(timespec='seconds')
        path = str(params.get('path') or '').strip()
        if path:
            if not Path(path).expanduser().is_absolute():
                raise ValueError('记录目标需要绝对路径')
            path = str(Path(path).expanduser().resolve())
        with self._lock, self._db() as db:
            jobs = db.execute('SELECT * FROM jobs WHERE subject=? AND owner=? ORDER BY rowid DESC', (subject, owner)).fetchall()
            job = next((r for r in jobs if (not path or r['path'] == path)), None)
            if action == 'export' and job and job['state'] != 'stopped':
                raise ValueError('该文件正在持续记录，请查看进度；未另行追加重复内容')
            if action in {'start', 'export'} and (job is None or job['state'] == 'stopped'):
                target = Path(path) if path else self.desktop / f'jishi-record-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}.txt'
                if target.suffix.lower() != '.txt' or not target.parent.is_dir():
                    raise ValueError('记录目标必须是已有目录内的TXT文件')
                if db.execute("SELECT 1 FROM jobs WHERE path=? AND state!='stopped'", (str(target),)).fetchone():
                    raise ValueError('该路径已有记录任务占用')
                prefix = target.read_bytes() if target.exists() else b''
                if len(prefix) > 8 * 1024 * 1024:
                    raise ValueError('已有文件超过8MB，未修改')
                latest = db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE subject=?', (subject,)).fetchone()[0]
                start = start_seq if start_seq and start_seq > 0 else latest+1
                if history == 'available' or since:
                    start = db.execute('SELECT MIN(seq) FROM events WHERE subject=? AND (?="all" OR owner=?) '
                        'AND (?="" OR stamp>=?)', (subject, scope, owner, since, since)).fetchone()[0] or latest+1
                elif start_seq:
                    anchor = db.execute('SELECT stamp FROM events WHERE subject=? AND seq=?', (subject, start_seq)).fetchone()
                    if anchor:
                        since = anchor[0]  # Late delivery replay must not import pre-request conversation.
                if action == 'export' and start > latest:
                    raise ValueError('所选范围没有可核验的对话原文，未创建空记录冒充成功')
                job_id = uuid4().hex
                db.execute('INSERT INTO jobs(id,subject,owner,work_id,path,scope,state,start_seq,stop_seq,cursor,prefix,content,error,include_replies,since,history) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (job_id, subject, owner, work_id, str(target), scope, 'running', start, latest if action == 'export' else None, start-1, prefix, prefix, '', include_replies, since, history))
                return self._flush(db, job_id)
            if job is None:
                raise ValueError('没有属于当前人物的记录任务')
            if action == 'status':
                return self._public(db, job['id'])
            if action == 'start' and (job['scope'] != scope or job['include_replies'] != include_replies):
                raise ValueError('已有记录的范围不同，请先停止再重新开始')
            if action in {'start', 'resume'}:
                db.execute('INSERT OR IGNORE INTO job_works VALUES(?,?)', (job['id'], work_id))
            if action == 'start' and (history == 'available' or since):
                earlier = db.execute('SELECT MIN(seq) FROM events WHERE subject=? AND (?="all" OR owner=?) '
                    'AND (?="" OR stamp>=?)', (subject, scope, owner, since, since)).fetchone()[0]
                if earlier is not None:
                    # An explicit backfill may widen a live recording, never erase its later lines.
                    lower = min(job['since'], since) if job['since'] and since else ''
                    db.execute('UPDATE jobs SET start_seq=?,since=?,history=? WHERE id=?',
                        (min(job['start_seq'], earlier), lower, history, job['id']))
            if action == 'stop' and job['stop_seq'] is None:
                latest = db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE subject=?', (subject,)).fetchone()[0]
                db.execute('UPDATE jobs SET stop_seq=? WHERE id=?', (latest, job['id']))
            if action == 'resume' and job['state'] == 'stopped':
                raise ValueError('已结束的记录请重新开始')
            return self._flush(db, job['id']) if job['state'] != 'stopped' else self._public(db, job['id'])

    def for_work(self, work_id):
        with self._lock, self._db() as db:
            ids = db.execute('SELECT id FROM jobs WHERE work_id=? OR EXISTS '
                '(SELECT 1 FROM job_works WHERE job_id=jobs.id AND work_id=?)', (work_id, work_id)).fetchall()
            return tuple(self._public(db, r[0]) for r in ids)

    def verify_running(self, subject):
        """Check durable output even when no new speech arrives; never overwrite a conflict."""
        changes = []
        with self._lock, self._db() as db:
            for row in db.execute('SELECT * FROM jobs WHERE subject=? AND state="running"', (subject,)).fetchall():
                try:
                    target = Path(row['path'])
                    if target.stat().st_size == len(row['content']) and target.read_bytes() == bytes(row['content']):
                        continue
                    error = '文件被外部改动，已暂停，未覆盖外部内容'
                except OSError as exc:
                    error = '记录文件无法回读，已暂停：' + type(exc).__name__
                db.execute('UPDATE jobs SET state="blocked",error=? WHERE id=?', (error, row['id']))
                changes.append(self._public(db, row['id']))
        if self.on_change:
            for row in changes:
                self.on_change(row)
        return tuple(changes)

    def list_for(self, subject, owner):
        with self._lock, self._db() as db:
            ids = db.execute('SELECT id FROM jobs WHERE subject=? AND owner=?', (subject, owner)).fetchall()
            return tuple(self._public(db, r[0]) for r in ids)

    def file_receipts(self, subject):
        """Artifact versions only, with no other person's conversation content."""
        with self._lock, self._db() as db:
            rows = db.execute('SELECT path,content,state FROM jobs WHERE subject=? ORDER BY rowid', (subject,)).fetchall()
            latest = {r['path']: r for r in rows}
            return tuple({'path': r['path'], 'bytes': len(r['content']), 'state': r['state'],
                          'sha256': hashlib.sha256(r['content']).hexdigest()}
                         for r in latest.values())

    @staticmethod
    def summary(row):
        state = {'running': '正在持续记录', 'stopped': '已停止并回读核验', 'blocked': '记录受阻'}[row['state']]
        gap = '启动前的部分输入已超出保留窗口，未确认全量记录。' if row.get('input_gap') else ''
        scope = '含宿主保留的近期原话，不保证完整历史。' if row.get('history') == 'available' else ''
        coverage = f"本段含用户原话{row.get('user_lines', 0)}条、匠石已交付回复{row.get('reply_lines', 0)}条。" if row['state'] != 'blocked' else ''
        excerpt = '已写入的用户原话摘录：' + json.dumps(row['excerpt'], ensure_ascii=False) if row.get('excerpt') else ''
        stopped = '本段已结束，后续没有持续记录。' if row['state'] == 'stopped' else ''
        return gap + scope + f"{state}；文件：{row['path']}；已写入位置：{row['cursor']}；{row['bytes']}字节。" + row['error'] + coverage + stopped + excerpt
