"""SQLite transactions keep snapshots and compressed media references atomic."""
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from time import time
from uuid import uuid4


class VisionStore:
    def __init__(self, root, config):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "vision.sqlite3"
        self.config = config
        with self.db() as c:
            c.execute('pragma auto_vacuum=full')
            c.executescript('''
                create table if not exists assets(id text primary key, data blob not null, size integer not null);
                create table if not exists evidence(frame text not null, module text not null, data text not null,
                    primary key(frame,module));
                create table if not exists media_sources(subject text not null, source_id text not null,
                    snapshot text not null, primary key(subject,source_id,snapshot));
                create table if not exists frames(id text primary key, subject text not null, source text not null,
                    captured real not null, received real not null, asset text not null);
                create table if not exists snapshots(id text primary key, subject text not null, frame text not null,
                    captured real not null, completed real not null, description text not null,
                    detections text not null, reason text not null, model text not null, described_at real not null,
                    description_frame text not null);
                create table if not exists current(subject text primary key, snapshot text not null);
                create table if not exists sample_current(subject text primary key, frame text not null);
                create table if not exists archives(frame text primary key);
                create table if not exists bindings(subject text not null, source_id text not null,
                    snapshot text not null, retain_photo integer not null, primary key(subject,source_id));
                create table if not exists events(subject text not null, event_id text not null,
                    snapshot text not null, retain_photo integer not null, primary key(subject,event_id,snapshot));
                create table if not exists delivered(subject text not null, snapshot text not null,
                    primary key(subject,snapshot));
                create table if not exists errors(at real not null, subject text not null, message text not null);
                create table if not exists calls(at real not null, subject text not null);
                create table if not exists inflight(frame text primary key);
                create table if not exists envelopes(frame text primary key, metadata text not null);
                create index if not exists frames_subject_time on frames(subject,captured);
            ''')
            columns = {r[1] for r in c.execute('pragma table_info(snapshots)')}
            if 'change_degree' not in columns:
                c.execute("alter table snapshots add column change_degree text not null default 'unknown'")
                c.execute("alter table snapshots add column change_reason text not null default ''")

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=10) as c:
            c.row_factory = sqlite3.Row
            yield c

    def prepare_image(self, data):
        from PIL import Image, ImageOps
        if not data or len(data) > self.config.max_image_bytes:
            raise ValueError("image exceeds upload limit")
        with Image.open(BytesIO(data)) as original:
            if original.width * original.height > 24_000_000:
                raise ValueError("image dimensions exceed limit")
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((1600, 1600))
            out = BytesIO()
            image.save(out, "JPEG", quality=82)
        return out.getvalue()

    def add_frame(self, subject, data, captured, source="visual", *, source_time=None, clock_offset=0, clock_uncertainty=0):
        if not subject or not source or not isinstance(captured, (int, float)):
            raise ValueError("invalid visual envelope")
        import math
        if not math.isfinite(captured) or captured <= 0 or captured > time() + 60:
            raise ValueError("invalid capture time")
        encoded = self.prepare_image(data)
        asset = sha256(encoded).hexdigest()
        frame = uuid4().hex
        from dataclasses import asdict
        from jshi.core.media import MediaEnvelope
        envelope = MediaEnvelope(source, 'image', captured, time(), frame,
                                 captured if source_time is None else source_time, clock_offset, clock_uncertainty)
        with self.db() as c:
            c.execute("insert or ignore into assets values(?,?,?)", (asset, encoded, len(encoded)))
            c.execute("insert into frames values(?,?,?,?,?,?)", (frame, subject, source, captured, time(), asset))
            c.execute('insert into envelopes values(?,?)', (frame,json.dumps(asdict(envelope))))
            c.execute('''insert into sample_current values(?,?) on conflict(subject) do update set frame=excluded.frame
                where (select captured from frames where id=excluded.frame) >=
                      (select captured from frames where id=sample_current.frame)''', (subject, frame))
        return frame

    def image(self, frame, subject):
        with self.db() as c:
            row = c.execute("select a.data from frames f join assets a on a.id=f.asset where f.id=? and f.subject=?",
                            (frame, subject)).fetchone()
        if row is None:
            raise KeyError("photo unavailable")
        return bytes(row[0])

    def add_evidence(self, subject, frame, rows):
        self.frame(frame, subject)
        with self.db() as c:
            for row in rows:
                if row['frame'] != frame:
                    raise ValueError('evidence belongs to another frame')
                c.execute('insert or replace into evidence values(?,?,?)',
                          (frame, row['module'], json.dumps(row, ensure_ascii=False)))

    def evidence(self, frame, subject):
        self.frame(frame, subject)
        with self.db() as c:
            return [json.loads(r[0]) for r in c.execute('select data from evidence where frame=? order by module', (frame,))]

    def frame(self, frame, subject):
        with self.db() as c:
            row = c.execute("select id,source,captured,received,asset from frames where id=? and subject=?", (frame, subject)).fetchone()
        if row is None:
            raise KeyError("photo unavailable")
        return dict(row)

    def publish(self, subject, frame, description, detections=(), reason="", model="", *, described_at=None, make_current=True, change_degree='unknown', change_reason=''):
        if not description.strip():
            raise ValueError("empty environment description")
        if change_degree not in ('unknown', 'none', 'low', 'medium', 'high'):
            raise ValueError('invalid environment change degree')
        sid = uuid4().hex
        with self.db() as c:
            f = c.execute("select captured from frames where id=? and subject=?", (frame, subject)).fetchone()
            if f is None:
                raise KeyError("photo unavailable")
            description_frame = frame
            described_at = f[0] if described_at is None else described_at
            latest = c.execute("select s.* from current u join snapshots s on s.id=u.snapshot where u.subject=?", (subject,)).fetchone()
            if make_current and latest and latest['captured'] > f[0]:
                if model and model != 'local' and latest['described_at'] <= described_at:
                    # A fresh cloud description can accompany a newer local sample;
                    # retain both photo references and their separate observation times.
                    frame = latest['frame']
                    f = (latest['captured'],)
                    detections = json.loads(latest['detections'])
                else:
                    return None
            if model == 'local' and latest:
                description_frame = latest['description_frame']
            c.execute("insert into snapshots(id,subject,frame,captured,completed,description,detections,reason,model,described_at,description_frame) values(?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, subject, frame, f[0], time(), description.strip(), json.dumps(detections, ensure_ascii=False), reason, model,
                       described_at, description_frame))
            c.execute('update snapshots set change_degree=?,change_reason=? where id=?', (change_degree, change_reason, sid))
            if make_current:
                c.execute("insert or replace into current values(?,?)", (subject, sid))
        return self.snapshot(sid, subject)

    def snapshot(self, sid, subject):
        with self.db() as c:
            row = c.execute("select * from snapshots where id=? and subject=?", (sid, subject)).fetchone()
            if row is None:
                return None
            return self._snapshot_row(c, row)

    @staticmethod
    def _snapshot_row(c, row):
        result = dict(row)
        result['image_available'] = bool(c.execute("select 1 from frames where id=? and subject=?", (row['frame'], row['subject'])).fetchone())
        result['detections'] = json.loads(result['detections'])
        result['analysis'] = [json.loads(r[0]) for r in c.execute('select data from evidence where frame=? order by module', (row['frame'],))]
        result['environment_experience'] = {'status':'not_implemented', 'references':[]}
        envelope = c.execute('select metadata from envelopes where frame=?', (result['frame'],)).fetchone()
        result['envelope'] = json.loads(envelope[0]) if envelope else None
        return result

    def latest(self, subject):
        with self.db() as c:
            row = c.execute("select snapshot from current where subject=?", (subject,)).fetchone()
        return self.snapshot(row[0], subject) if row else None

    def latest_sample(self, subject):
        """Latest captured photo with separately dated completed understanding."""
        with self.db() as c:
            # Selection and snapshot creation must share a transaction. Otherwise
            # the sampler can replace/delete this frame before publish reads it.
            c.execute('begin immediate')
            current = c.execute('select s.* from current u join snapshots s on s.id=u.snapshot where u.subject=?', (subject,)).fetchone()
            sample = c.execute('select f.id,f.captured from sample_current u join frames f on f.id=u.frame where u.subject=?', (subject,)).fetchone()
            if not sample or (current and current['frame'] == sample['id']):
                return self._snapshot_row(c, current) if current else None
            frame = sample['id']
            description = current['description'] if current else '尚未完成线上环境描述。'
            existing = c.execute('select * from snapshots where subject=? and frame=? and description=? and reason=? order by completed desc limit 1',
                                 (subject, frame, description, 'sample')).fetchone()
            if existing:
                return self._snapshot_row(c, existing)
            sid = uuid4().hex
            c.execute('insert into snapshots(id,subject,frame,captured,completed,description,detections,reason,model,described_at,description_frame) values(?,?,?,?,?,?,?,?,?,?,?)', (
                sid, subject, frame, sample['captured'], time(), description, '[]', 'sample', 'local',
                current['described_at'] if current else 0,
                current['description_frame'] if current else frame))
            if current:
                c.execute('update snapshots set change_degree=?,change_reason=? where id=?', (current['change_degree'], current['change_reason'], sid))
            return self._snapshot_row(c, c.execute('select * from snapshots where id=?', (sid,)).fetchone())

    def memories(self, subject, limit=30):
        with self.db() as c:
            ids = [r[0] for r in c.execute('''select distinct s.id from snapshots s
                where s.subject=? and s.id in (select snapshot from bindings where subject=?
                union select snapshot from events where subject=?) order by s.captured desc limit ?''', (subject, subject, subject, limit))]
        return [self.snapshot(sid, subject) for sid in ids]

    def bind(self, subject, source_id, sid, *, photo_candidate=False):
        with self.db() as c:
            c.execute('begin immediate')
            s = c.execute("select frame,description_frame,change_degree from snapshots where id=? and subject=?", (sid, subject)).fetchone()
            if s is None:
                return False
            pinned = {r[0] for r in c.execute('''select distinct f.asset from frames f join snapshots s
                on s.description_frame=f.id where s.id in
                (select snapshot from bindings where retain_photo=1 union select snapshot from events where retain_photo=1)''')}
            wanted = {r[0] for r in c.execute('select asset from frames where id=?', (s[1],))}
            sizes = {r[0]:r[1] for r in c.execute('select id,size from assets')}
            retained = (photo_candidate or s[2] in ('medium', 'high')) and bool(wanted) and (wanted <= pinned or sum(sizes[a] for a in pinned | wanted) <= self.config.memory_bytes)
            c.execute("insert or ignore into bindings values(?,?,?,?)", (subject, source_id, sid, int(retained)))
        return retained

    def record_main_images(self, subject, source_ids, frames):
        for frame in dict.fromkeys(frames):
            with self.db() as c:
                existing = c.execute("select id from snapshots where subject=? and frame=? and reason='main_image' order by completed desc limit 1", (subject,frame)).fetchone()
            snapshot = self.snapshot(existing[0],subject) if existing else self.publish(
                subject, frame, '主认知实际接收的图片；未由此记录补造视觉描述。',
                reason='main_image', described_at=0, make_current=False)
            if not snapshot:
                continue
            for source_id in source_ids:
                # One input may reference several photos; distinct keys keep them all.
                self.bind(subject, source_id + ':photo:' + frame, snapshot['id'], photo_candidate=True)
            with self.db() as c:
                for source_id in source_ids:
                    c.execute('insert or ignore into media_sources values(?,?,?)', (subject, source_id, snapshot['id']))

    def associated(self, subject, source_ids=(), event_id=""):
        return self.associated_many(subject, [(source_ids, event_id)])[0]

    def associated_many(self, subject, requests, *, images_only=False):
        """Resolve only explicit associations, using one connection for the batch."""
        results = []
        with self.db() as c:
            cache = {}
            for source_ids, event_id in requests:
                photo_filter = ' and retain_photo=1' if images_only else ''
                ids = {r[0] for r in c.execute("select snapshot from events where subject=? and event_id=?"+photo_filter, (subject, event_id))} if event_id else set()
                for source in source_ids:
                    ids.update(r[0] for r in c.execute("select snapshot from bindings where subject=? and source_id=?"+photo_filter, (subject, source)))
                    ids.update(r[0] for r in c.execute('select m.snapshot from media_sources m where m.subject=? and m.source_id=?' + (
                        ' and exists(select 1 from bindings b where b.subject=m.subject and b.snapshot=m.snapshot and b.retain_photo=1)' if images_only else ''), (subject,source)))
                snapshots = []
                for sid in ids:
                    if sid not in cache:
                        row = c.execute('select * from snapshots where id=? and subject=?', (sid, subject)).fetchone()
                        cache[sid] = self._snapshot_row(c, row) if row else None
                    s = cache[sid]
                    if s and images_only:
                        photo = c.execute('select captured from frames where id=? and subject=?', (s['description_frame'], subject)).fetchone()
                        s = {**s, 'frame':s['description_frame'], 'captured':photo[0], 'image_available':True} if photo else None
                    if s:
                        snapshots.append(s)
                # Repeated samples sharing one completed scene description need
                # one representative photo, rather than repeated scene paragraphs.
                unique = {}
                for s in sorted(snapshots, key=lambda s: (s['captured'], s['completed'])):
                    unique[(s['description_frame'], s['description'])] = s
                results.append(list(unique.values()))
        return results

    def record_delivery(self, subject, snapshots, stored_marks, media_marks=None, *, finished=True):
        with self.db() as c:
            for segment, ss in snapshots.items():
                for event in stored_marks.get(segment, ()):
                    for s in ss:
                        retained = c.execute('select max(retain_photo) from bindings where subject=? and snapshot=?', (subject,s['id'])).fetchone()[0] or 0
                        if media_marks is not None:
                            retained = retained and s['frame'] in media_marks.get(segment, ())
                        c.execute("insert into events values(?,?,?,?) on conflict(subject,event_id,snapshot) do update set retain_photo=excluded.retain_photo", (subject, event, s['id'], retained))
                        c.execute("insert or ignore into delivered values(?,?)", (subject, s['id']))
                if media_marks is not None:
                    for s in ss:
                        if s['frame'] not in media_marks.get(segment, ()):
                            c.execute('update bindings set retain_photo=0 where subject=? and snapshot=?', (subject,s['id']))
                elif finished and not stored_marks.get(segment):
                    for s in ss:
                        if s['reason'] == 'main_image':
                            c.execute('update bindings set retain_photo=0 where subject=? and snapshot=?', (subject,s['id']))

    def errors(self, subject):
        with self.db() as c:
            return [dict(r) for r in c.execute('select * from errors where subject=? order by at desc limit 10', (subject,))]

    def claim_call(self, subject):
        with self.db() as c:
            c.execute('begin immediate')
            c.execute('delete from calls where at<?', (time()-3600,))
            count = c.execute('select count(*) from calls where subject=?', (subject,)).fetchone()[0]
            if count >= self.config.max_calls_per_hour:
                return False
            c.execute('insert into calls values(?,?)', (time(), subject))
        return True

    def delivered(self, subject, sid):
        with self.db() as c:
            return bool(c.execute("select 1 from delivered where subject=? and snapshot=?", (subject, sid)).fetchone())

    def description_delivered(self, subject, description, described_at):
        with self.db() as c:
            return bool(c.execute('''select 1 from snapshots s join delivered d on d.snapshot=s.id
                where d.subject=? and s.description=?''', (subject, description)).fetchone())

    def discard_frame(self, subject, frame):
        with self.db() as c:
            c.execute("delete from frames where subject=? and id=? and id not in (select frame from snapshots union select frame from sample_current union select frame from archives union select frame from inflight)", (subject, frame))
            c.execute("delete from assets where id not in (select asset from frames)")
            c.execute('delete from envelopes where frame not in (select id from frames)')
            c.execute('delete from evidence where frame not in (select id from frames)')

    def archive(self, frame):
        with self.db() as c:
            c.execute('insert or ignore into archives values(?)', (frame,))

    def error(self, subject, message):
        with self.db() as c:
            c.execute("insert into errors values(?,?,?)", (time(), subject, str(message)[:500]))
            c.execute("delete from errors where rowid not in (select rowid from errors order by rowid desc limit 100)")

    def cleanup(self):
        with self.db() as c:
            c.execute('begin immediate')
            protected = {r[0] for r in c.execute('''select frame from snapshots where id in
                (select snapshot from current)''')}
            protected.update(r[0] for r in c.execute('''select description_frame from snapshots where id in
                (select snapshot from current union select snapshot from bindings where retain_photo=1 union select snapshot from events where retain_photo=1)'''))
            protected.update(r[0] for r in c.execute('select frame from inflight'))
            protected.update(r[0] for r in c.execute('select frame from sample_current'))
            rows = list(c.execute("select f.id,f.captured,a.size from frames f join assets a on a.id=f.asset order by f.captured desc"))
            used = 0
            keep = set(protected)
            for r in rows:
                if r['id'] in protected:
                    continue
                if time() - r['captured'] <= self.config.retention_seconds and used + r['size'] <= self.config.rolling_bytes:
                    used += r['size']
                    keep.add(r['id'])
            for r in rows:
                if r['id'] not in keep:
                    c.execute("delete from frames where id=?", (r['id'],))
            c.execute("delete from assets where id not in (select asset from frames)")
            c.execute('delete from envelopes where frame not in (select id from frames)')
            c.execute('delete from archives where frame not in (select id from frames)')
            c.execute('''delete from snapshots where frame not in (select id from frames) and
                id not in (select snapshot from current union select snapshot from bindings union select snapshot from events)''')

    def protect(self, frame, active=True):
        with self.db() as c:
            if active:
                c.execute('insert or ignore into inflight values(?)', (frame,))
            else:
                c.execute('delete from inflight where frame=?', (frame,))

    @staticmethod
    def render(s, object_codes=()):
        from datetime import datetime, timezone
        at = datetime.fromtimestamp(s['captured'], timezone.utc).isoformat(timespec='seconds')
        described = datetime.fromtimestamp(s['described_at'], timezone.utc).isoformat(timespec='seconds') if s['described_at'] else '尚未完成'
        codes = {oid:code for code,oid in object_codes}
        analysis = []
        for row in s.get('analysis', ()):
            value = {k:row[k] for k in ('module', 'observed_at', 'status', 'result') if k in row}
            if isinstance(value.get('observed_at'), (int,float)):
                value['observed_at'] = round(value['observed_at']) if value['observed_at'] > 0 else None
            if isinstance(value.get('result'), dict) and 'faces' in value['result']:
                value['result'] = {**value['result'], 'faces':[
                    {**{k:v for k,v in face.items() if k != 'object_id'},
                     **({'person':codes[face['object_id']]} if face.get('object_id') in codes else {})}
                    for face in value['result']['faces']]}
            analysis.append(value)
        return f"【环境观察 {at}；描述依据 {described}；环境变化 {s.get('change_degree', 'unknown')}】\n{s['description']}\n" + (
            "【本地检测】" + json.dumps(s['detections'], ensure_ascii=False) if s['detections'] else "") + (
            '\n【视觉模块证据】' + json.dumps(analysis, ensure_ascii=False) if analysis else '')
