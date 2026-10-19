"""Persistent, independently labelled audio for personal speaker classifiers."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from threading import RLock
from time import time

OTHER_VOICE = '__negative_other__'  # sample category, never a person/profile/model target


DEFAULTS = {
    'classifier_mode': 'independent_binary',
    'enabled': True, 'budget': 20, 'batch_samples': 10, 'auto_score': .50, 'auto_margin': .10,
    'min_train_positive': 12, 'min_train_negative': 24,
    'min_eval_positive': 4, 'min_eval_negative': 12,
    'max_false_accept': .02, 'min_recall': .60, 'min_threshold': .80,
    'conflict_margin': .10, 'max_audio_bytes': 256 * 1024 * 1024,
}


class PersonalStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'samples.sqlite3'
        self.lock = RLock()
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS targets(
                    object_id TEXT PRIMARY KEY, pinned INTEGER NOT NULL, source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'collecting', detail TEXT NOT NULL DEFAULT '',
                    attempted_revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS samples(
                    id TEXT PRIMARY KEY, object_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    input_id TEXT NOT NULL, source TEXT NOT NULL, score REAL,
                    pcm BLOB NOT NULL, revision INTEGER NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS features(
                    sample_id TEXT NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
                    model_id TEXT NOT NULL, dimension INTEGER NOT NULL, vectors TEXT NOT NULL,
                    durations TEXT NOT NULL, PRIMARY KEY(sample_id, model_id));
                CREATE INDEX IF NOT EXISTS samples_person ON samples(object_id);
                CREATE TABLE IF NOT EXISTS excluded_samples(id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS background_samples(
                    id TEXT PRIMARY KEY, dataset TEXT NOT NULL, speaker TEXT NOT NULL,
                    clip TEXT NOT NULL, session_id TEXT NOT NULL, pcm BLOB NOT NULL,
                    source_url TEXT NOT NULL, license TEXT NOT NULL, sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS background_features(
                    sample_id TEXT NOT NULL REFERENCES background_samples(id) ON DELETE CASCADE,
                    model_id TEXT NOT NULL, dimension INTEGER NOT NULL, vectors TEXT NOT NULL,
                    durations TEXT NOT NULL, PRIMARY KEY(sample_id, model_id));
            ''')
            for key, value in [('settings', DEFAULTS), ('revision', 0), ('label_epoch', 0)]:
                db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', (key, json.dumps(value)))
            settings = self.value(db, 'settings')
            if db.execute("SELECT 1 FROM meta WHERE key='small_sample_training'").fetchone() is None:
                if settings.get('batch_samples') == 16:
                    settings['batch_samples'] = 10
                    self.put(db, 'settings', settings)
                self.put(db, 'small_sample_training', 'v1')
            if 'auto_margin' not in settings:
                # Upgrade the previous default while retaining non-default custom scores.
                if settings.get('auto_score') == .85:
                    settings['auto_score'] = DEFAULTS['auto_score']
                settings['auto_margin'] = DEFAULTS['auto_margin']
                self.put(db, 'settings', settings)
            if db.execute("SELECT 1 FROM meta WHERE key='negative_policy'").fetchone() is None:
                people = db.execute('SELECT COUNT(DISTINCT object_id) FROM samples').fetchone()[0]
                automatic = db.execute("SELECT COUNT(*) FROM samples WHERE source!='manual'").fetchone()[0]
                if people > 1 and automatic:
                    self.put(db, 'label_epoch', self.value(db, 'label_epoch') + 1)
                self.put(db, 'negative_policy', 'explicit_other_person_v1')

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            try:
                with db:
                    yield db
            finally:
                db.close()

    @staticmethod
    def value(db, key):
        return json.loads(db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0])

    @staticmethod
    def put(db, key, value):
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))

    def settings(self):
        with self.connect() as db:
            return {**DEFAULTS, **self.value(db, 'settings')}

    def configure(self, changes):
        if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
            raise ValueError('未知个人分类设置')
        config = {**self.settings(), **changes}
        for key, value in config.items():
            if key == 'classifier_mode':
                if value != 'independent_binary':
                    raise ValueError('当前仅实现独立二分类；多分类接口预留，尚未实现训练')
            elif key == 'enabled':
                if type(value) is not bool:
                    raise ValueError('enabled 须为布尔值')
            elif key in {'auto_score', 'auto_margin', 'max_false_accept', 'min_recall', 'min_threshold', 'conflict_margin'}:
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError(f'{key} 须在 0–1 之间')
            elif type(value) is not int or value < 1:
                raise ValueError(f'{key} 须为正整数')
        if config['budget'] > 200 or config['batch_samples'] > 10000 or config['max_audio_bytes'] > 2**31:
            raise ValueError('预算、批量或存储上限过大')
        with self.connect() as db:
            if db.execute('SELECT COUNT(*) FROM targets').fetchone()[0] > config['budget']:
                raise ValueError('请先移除部分目标人物，再降低预算')
            self.put(db, 'settings', config)
        return config

    def targets(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM targets ORDER BY rowid')]

    def select(self, object_ids, *, source='manual', pinned=True):
        """The shared manual/JiangShi selection interface; automatic calls preserve pins."""
        if source not in {'manual', 'jiangshi'} or type(pinned) is not bool:
            raise ValueError('无效的人物选择来源')
        ids = list(dict.fromkeys(object_ids))
        if any(not isinstance(oid, str) or not oid or len(oid) > 200 for oid in ids):
            raise ValueError('无效人物编号')
        with self.connect() as db:
            old = {r['object_id']: dict(r) for r in db.execute('SELECT * FROM targets')}
            preserved = [oid for oid, r in old.items() if source == 'jiangshi' and r['pinned']]
            chosen = list(dict.fromkeys([*preserved, *ids]))
            if len(chosen) > self.value(db, 'settings')['budget']:
                raise ValueError('目标人物超过预算；自动选择还须为人工固定人物预留名额')
            for oid in old:
                if oid not in chosen:
                    db.execute('DELETE FROM targets WHERE object_id=?', (oid,))
            for oid in chosen:
                if oid in preserved:
                    continue
                db.execute('''INSERT INTO targets(object_id,pinned,source) VALUES(?,?,?)
                    ON CONFLICT(object_id) DO UPDATE SET pinned=excluded.pinned,source=excluded.source''',
                    (oid, int(pinned if source == 'manual' else False), source))
        return self.targets()

    def revision(self):
        with self.connect() as db:
            return self.value(db, 'revision'), self.value(db, 'label_epoch')

    @staticmethod
    def sample_id(session_id, input_id):
        return hashlib.sha256(json.dumps([session_id, input_id], ensure_ascii=False).encode()).hexdigest()

    def save(self, pcm, object_id, session_id, input_id, *, source, score=None, overlap=False):
        if overlap:
            raise ValueError('重叠声音不能作为单人训练样本')
        if source not in {'manual', 'voiceprint'}:
            raise ValueError('训练样本只接受人工或原声纹标注')
        if not all(isinstance(x, str) and x and len(x) <= 200 for x in (object_id, session_id, input_id)):
            raise ValueError('训练样本缺少有效人物、会话或输入编号')
        if not isinstance(pcm, bytes) or len(pcm) % 2 or not 6400 <= len(pcm) <= 30 * 32000:
            raise ValueError('声音须为 0.2–30 秒、16k 单声道 PCM')
        if source == 'voiceprint' and (type(score) not in (int, float) or not math.isfinite(score)
                                      or score < self.settings()['auto_score'] or score > 1):
            raise ValueError('原声纹分数未达自动采集门槛')
        sid = self.sample_id(session_id, input_id)
        with self.connect() as db:
            if source != 'manual' and db.execute('SELECT 1 FROM excluded_samples WHERE id=?', (sid,)).fetchone():
                return None
            if source == 'manual':
                db.execute('DELETE FROM excluded_samples WHERE id=?', (sid,))
            previous = db.execute('SELECT * FROM samples WHERE id=?', (sid,)).fetchone()
            if previous and previous['source'] == 'manual' and source != 'manual':
                return sid
            if previous and previous['object_id'] == object_id and previous['source'] == source and previous['pcm'] == pcm:
                return sid
            used = db.execute('SELECT COALESCE(SUM(length(pcm)),0) FROM samples').fetchone()[0]
            used += db.execute('SELECT COALESCE(SUM(length(pcm)),0) FROM background_samples').fetchone()[0]
            if used - (len(previous['pcm']) if previous else 0) + len(pcm) > self.value(db, 'settings')['max_audio_bytes']:
                raise ValueError('个人声音样本库已达到容量上限，请删除旧样本')
            revision = self.value(db, 'revision') + 1
            self.put(db, 'revision', revision)
            if previous and previous['object_id'] != object_id:
                self.put(db, 'label_epoch', self.value(db, 'label_epoch') + 1)
            if previous and previous['pcm'] != pcm:
                db.execute('DELETE FROM features WHERE sample_id=?', (sid,))
            db.execute('''INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET object_id=excluded.object_id,source=excluded.source,
                score=excluded.score,pcm=excluded.pcm,revision=excluded.revision''',
                (sid, object_id, session_id, input_id, source, score, pcm, revision, time()))
        return sid

    def relabel(self, sid, object_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM samples WHERE id=?', (sid,)).fetchone()
            if not row:
                raise ValueError('样本不存在')
            row = dict(row)
        return self.save(row['pcm'], object_id, row['session_id'], row['input_id'], source='manual')

    def delete(self, sid):
        with self.connect() as db:
            if db.execute('DELETE FROM samples WHERE id=?', (sid,)).rowcount:
                self.put(db, 'label_epoch', self.value(db, 'label_epoch') + 1)
                self.put(db, 'revision', self.value(db, 'revision') + 1)

    def withdraw(self, session_id, input_id):
        """Remove a recording and prevent delayed automatic collection from restoring it."""
        sid = self.sample_id(session_id, input_id)
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO excluded_samples VALUES(?)', (sid,))
            if db.execute('DELETE FROM samples WHERE id=?', (sid,)).rowcount:
                self.put(db, 'label_epoch', self.value(db, 'label_epoch') + 1)
                self.put(db, 'revision', self.value(db, 'revision') + 1)

    def samples(self, *, audio=False, limit=10000):
        with self.connect() as db:
            columns = '*' if audio else 'id,object_id,session_id,input_id,source,score,revision,created,length(pcm)/32000.0 AS seconds'
            return [dict(r) for r in db.execute(f'SELECT {columns} FROM samples ORDER BY revision DESC LIMIT ?', (limit,))]

    def sample(self, sid):
        with self.connect() as db:
            row = db.execute('SELECT id,object_id,source,score FROM samples WHERE id=?', (sid,)).fetchone()
            return dict(row) if row else None

    def features(self, sid, model_id, *, background=False):
        with self.connect() as db:
            table = 'background_features' if background else 'features'
            row = db.execute(f'SELECT * FROM {table} WHERE sample_id=? AND model_id=?', (sid, model_id)).fetchone()
            if not row:
                return None
            return {'vectors': json.loads(row['vectors']), 'durations': json.loads(row['durations']), 'dimension': row['dimension']}

    def cache(self, sid, model_id, vectors, durations, *, background=False):
        with self.connect() as db:
            table = 'background_features' if background else 'features'
            db.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?,?,?,?)',
                       (sid, model_id, len(vectors[0]), json.dumps(vectors), json.dumps(durations)))

    def save_background(self, pcm, *, dataset, speaker, clip, source_url, license):
        """Explicitly imported public speakers; never create local person profiles."""
        if not all(isinstance(v, str) and v and len(v) <= 500 for v in (dataset, speaker, clip, source_url, license)):
            raise ValueError('公共反例须保留语料、说话人、录音编号、来源及许可')
        if not source_url.startswith('https://') or license not in {'Apache-2.0', 'CC-BY-4.0', 'CC0-1.0'}:
            raise ValueError('公共反例缺少支持的来源或许可')
        if not isinstance(pcm, bytes) or len(pcm) % 2 or not 6400 <= len(pcm) <= 30*32000:
            raise ValueError('公共录音须为0.2–30秒的16k单声道PCM')
        sid = hashlib.sha256(json.dumps([dataset, clip], ensure_ascii=False).encode()).hexdigest()
        # All utterances of a public speaker stay on the same side of the split.
        session_id = 'public:' + dataset + ':' + speaker
        digest = hashlib.sha256(pcm).hexdigest()
        with self.connect() as db:
            old = db.execute('SELECT speaker,sha256 FROM background_samples WHERE id=?', (sid,)).fetchone()
            if old:
                if old['speaker'] != speaker or old['sha256'] != digest:
                    raise ValueError('同一公共录音编号的内容或说话人不一致')
                return sid
            used = db.execute('SELECT COALESCE(SUM(length(pcm)),0) FROM samples').fetchone()[0]
            used += db.execute('SELECT COALESCE(SUM(length(pcm)),0) FROM background_samples').fetchone()[0]
            if used+len(pcm) > self.value(db, 'settings')['max_audio_bytes']:
                raise ValueError('个人声音样本库已达到容量上限')
            revision = self.value(db, 'revision') + 1
            self.put(db, 'revision', revision)
            db.execute('INSERT INTO background_samples VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (sid, dataset, speaker, clip, session_id, pcm, source_url, license, digest, revision, time()))
        return sid

    def background_samples(self, *, audio=False):
        with self.connect() as db:
            columns = '*' if audio else 'id,dataset,speaker,clip,session_id,source_url,license,revision,length(pcm)/32000.0 AS seconds'
            return [dict(r) for r in db.execute(f'SELECT {columns} FROM background_samples ORDER BY id')]

    def state(self, oid, status, detail='', *, attempted_revision=None):
        with self.connect() as db:
            db.execute('UPDATE targets SET status=?,detail=? WHERE object_id=?', (status, str(detail)[:500], oid))
            if attempted_revision is not None:
                db.execute('UPDATE targets SET attempted_revision=? WHERE object_id=?', (attempted_revision, oid))

    def counts(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('''SELECT object_id,source,COUNT(*) AS clips,
                SUM(length(pcm))/32000.0 AS seconds FROM samples GROUP BY object_id,source''')]
