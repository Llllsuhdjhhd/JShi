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

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=10) as c:
            c.row_factory = sqlite3.Row
            yield c

    def add_frame(self, subject, data, captured, source="visual", *, source_time=None, clock_offset=0, clock_uncertainty=0):
        from PIL import Image, ImageOps
        if not subject or not source or not isinstance(captured, (int, float)):
            raise ValueError("invalid visual envelope")
        import math
        if not math.isfinite(captured) or captured <= 0 or captured > time() + 60:
            raise ValueError("invalid capture time")
        if not data or len(data) > self.config.max_image_bytes:
            raise ValueError("image exceeds upload limit")
        with Image.open(BytesIO(data)) as original:
            if original.width * original.height > 24_000_000:
                raise ValueError("image dimensions exceed limit")
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((1600, 1600))
            out = BytesIO()
            image.save(out, "JPEG", quality=82)
        encoded = out.getvalue()
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

    def frame(self, frame, subject):
        with self.db() as c:
            row = c.execute("select id,source,captured,received,asset from frames where id=? and subject=?", (frame, subject)).fetchone()
        if row is None:
            raise KeyError("photo unavailable")
        return dict(row)

    def publish(self, subject, frame, description, detections=(), reason="", model="", *, described_at=None, make_current=True):
        if not description.strip():
            raise ValueError("empty environment description")
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
            c.execute("insert into snapshots values(?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, subject, frame, f[0], time(), description.strip(), json.dumps(detections, ensure_ascii=False), reason, model,
                       described_at, description_frame))
            if make_current:
                c.execute("insert or replace into current values(?,?)", (subject, sid))
        return self.snapshot(sid, subject)

    def snapshot(self, sid, subject):
        with self.db() as c:
            row = c.execute("select * from snapshots where id=? and subject=?", (sid, subject)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result['image_available'] = bool(c.execute("select 1 from frames where id=? and subject=?", (row['frame'], subject)).fetchone())
        result['detections'] = json.loads(result['detections'])
        with self.db() as c:
            envelope = c.execute('select metadata from envelopes where frame=?', (result['frame'],)).fetchone()
        result['envelope'] = json.loads(envelope[0]) if envelope else None
        return result

    def latest(self, subject):
        with self.db() as c:
            row = c.execute("select snapshot from current where subject=?", (subject,)).fetchone()
        return self.snapshot(row[0], subject) if row else None

    def latest_sample(self, subject):
        """Latest captured photo with separately dated completed understanding."""
        current = self.latest(subject)
        with self.db() as c:
            row = c.execute('select frame from sample_current where subject=?', (subject,)).fetchone()
        if not row:
            return current
        frame = row[0]
        if current and current['frame'] == frame:
            return current
        description = current['description'] if current else '尚未完成线上环境描述。'
        with self.db() as c:
            existing = c.execute('select id from snapshots where subject=? and frame=? and description=? and reason=? order by completed desc limit 1',
                                 (subject, frame, description, 'sample')).fetchone()
        if existing:
            return self.snapshot(existing[0], subject)
        return self.publish(subject, frame, description, reason='sample', model='local',
                            described_at=current['described_at'] if current else 0, make_current=False)

    def memories(self, subject, limit=30):
        with self.db() as c:
            ids = [r[0] for r in c.execute('''select distinct s.id from snapshots s
                where s.subject=? and s.id in (select snapshot from bindings where subject=?
                union select snapshot from events where subject=?) order by s.captured desc limit ?''', (subject, subject, subject, limit))]
        return [self.snapshot(sid, subject) for sid in ids]

    def bind(self, subject, source_id, sid):
        with self.db() as c:
            c.execute('begin immediate')
            s = c.execute("select frame,description_frame from snapshots where id=? and subject=?", (sid, subject)).fetchone()
            if s is None:
                return False
            pinned = {r[0] for r in c.execute('''select distinct f.asset from frames f join snapshots s
                on s.frame=f.id or s.description_frame=f.id where s.id in
                (select snapshot from bindings where retain_photo=1 union select snapshot from events where retain_photo=1)''')}
            wanted = {r[0] for r in c.execute('select asset from frames where id in (?,?)', (s[0],s[1]))}
            sizes = {r[0]:r[1] for r in c.execute('select id,size from assets')}
            retained = bool(wanted) and (wanted <= pinned or sum(sizes[a] for a in pinned | wanted) <= self.config.memory_bytes)
            c.execute("insert or ignore into bindings values(?,?,?,?)", (subject, source_id, sid, int(retained)))
        return retained

    def associated(self, subject, source_ids=(), event_id=""):
        with self.db() as c:
            ids = {r[0] for r in c.execute("select snapshot from events where subject=? and event_id=?", (subject, event_id))}
            for source in source_ids:
                ids.update(r[0] for r in c.execute("select snapshot from bindings where subject=? and source_id=?", (subject, source)))
        return [s for sid in sorted(ids) if (s := self.snapshot(sid, subject))]

    def record_delivery(self, subject, snapshots, stored_marks):
        with self.db() as c:
            for segment, ss in snapshots.items():
                for event in stored_marks.get(segment, ()):
                    for s in ss:
                        retained = c.execute('select max(retain_photo) from bindings where subject=? and snapshot=?', (subject,s['id'])).fetchone()[0] or 0
                        c.execute("insert or ignore into events values(?,?,?,?)", (subject, event, s['id'], retained))
                        c.execute("insert or ignore into delivered values(?,?)", (subject, s['id']))

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

    def archive(self, frame):
        with self.db() as c:
            c.execute('insert or ignore into archives values(?)', (frame,))

    def error(self, subject, message):
        with self.db() as c:
            c.execute("insert into errors values(?,?,?)", (time(), subject, str(message)[:500]))
            c.execute("delete from errors where rowid not in (select rowid from errors order by rowid desc limit 100)")

    def cleanup(self):
        with self.db() as c:
            protected = {r[0] for r in c.execute('''select frame from snapshots where id in
                (select snapshot from current union select snapshot from bindings where retain_photo=1 union select snapshot from events where retain_photo=1)''')}
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
    def render(s):
        from datetime import datetime, timezone
        at = datetime.fromtimestamp(s['captured'], timezone.utc).isoformat()
        described = datetime.fromtimestamp(s['described_at'], timezone.utc).isoformat() if s['described_at'] else '尚未完成'
        return f"【环境观察 {at}；版本 {s['id']}；描述依据 {described}】\n{s['description']}\n" + (
            "【本地检测变化】" + json.dumps(s['detections'], ensure_ascii=False) if s['detections'] else "")
