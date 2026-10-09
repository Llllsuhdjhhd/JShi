"""Explicitly bound person photos, separate from transient camera samples."""
from io import BytesIO
from pathlib import Path
import sqlite3
from time import time
from hashlib import sha256
import json


class PersonPhotos:
    def __init__(self, root):
        self.path = Path(root) / 'person_photos.sqlite3'

    def get(self, subject_id, object_id):
        if not self.path.is_file():
            return None
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3) as c:
            if c.execute("select 1 from sqlite_master where name='person_photo_history'").fetchone():
                row = c.execute("select image from person_photo_history where subject_id=? and object_id=? and status='confirmed' order by last_seen desc limit 1", (subject_id, object_id)).fetchone()
                if row: return bytes(row[0])
            row = c.execute('select image from person_photos where subject_id=? and object_id=?',
                            (subject_id, object_id)).fetchone()
        return bytes(row[0]) if row else None

    def put(self, subject_id, object_id, data, *, captured=None, frame='', snapshot='',
            box=None, basis='manual', evidence=(), status='confirmed', event_ids=()):
        from PIL import Image, ImageOps
        if not data or len(data) > 8_000_000:
            raise ValueError('人物照片须小于 8 MB')
        with Image.open(BytesIO(data)) as original:
            if original.width * original.height > 24_000_000:
                raise ValueError('人物照片尺寸过大')
            image = ImageOps.exif_transpose(original).convert('RGB')
            if box is not None:
                if len(box) != 4 or not all(isinstance(v,(int,float)) and 0 <= v <= 1 for v in box) or not (box[0] < box[2] and box[1] < box[3]):
                    raise ValueError('人物照片区域无效')
                image = image.crop((int(box[0]*image.width),int(box[1]*image.height),int(box[2]*image.width),int(box[3]*image.height)))
                if min(image.size) < 8: raise ValueError('人物照片区域过小')
            image.thumbnail((1200, 1200))
            out = BytesIO()
            image.save(out, 'JPEG', quality=85)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if status not in {'confirmed','candidate'}: raise ValueError('人物照片状态无效')
        captured = time() if captured is None else float(captured)
        import math
        if not math.isfinite(captured) or captured <= 0: raise ValueError('人物照片时间无效')
        encoded = out.getvalue()
        photo_id = sha256((subject_id+'\0'+object_id+'\0').encode()+encoded).hexdigest()
        with sqlite3.connect(self.path, timeout=3) as c:
            c.execute('create table if not exists person_photos(subject_id text, object_id text, image blob not null, updated real not null, primary key(subject_id,object_id))')
            c.execute('''create table if not exists person_photo_history(id text primary key, subject_id text,
                object_id text, image blob, captured real, last_seen real, frame text, snapshot text,
                box text, basis text, evidence text, status text, event_ids text)''')
            # Migrate previously selected photos without losing them on the first update.
            for subject, obj, old, at in c.execute('select * from person_photos').fetchall():
                old_id = sha256((subject+'\0'+obj+'\0').encode()+old).hexdigest()
                c.execute('insert or ignore into person_photo_history values(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (old_id,subject,obj,old,at,at,'','', 'null','manual','[]','confirmed','[]'))
            existing = c.execute('select status,event_ids,evidence from person_photo_history where id=?',(photo_id,)).fetchone()
            if not existing and c.execute('select coalesce(sum(length(image)),0) from person_photo_history').fetchone()[0] + len(encoded) > 512*1024*1024:
                raise ValueError('人物照片记忆已达到512 MB上限')
            if existing:
                status = 'confirmed' if 'confirmed' in (status,existing[0]) else 'candidate'
                event_ids = sorted(set(event_ids)|set(json.loads(existing[1])))
                evidence = list(dict.fromkeys([*json.loads(existing[2]),*evidence]))
            c.execute('''insert into person_photo_history values(?,?,?,?,?,?,?,?,?,?,?,?,?) on conflict(id)
                do update set last_seen=max(last_seen,excluded.last_seen),status=excluded.status,
                event_ids=excluded.event_ids,evidence=excluded.evidence''',
                (photo_id,subject_id,object_id,encoded,captured,captured,frame,snapshot,json.dumps(box),basis,json.dumps(evidence,ensure_ascii=False),status,json.dumps(list(event_ids))))
            if status == 'confirmed':
                c.execute('insert or replace into person_photos values(?,?,?,?)',(subject_id,object_id,encoded,time()))
        return photo_id

    def list(self, subject_id, object_id, *, confirmed_only=False):
        if not self.path.is_file(): return []
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3) as c:
            c.row_factory = sqlite3.Row
            if not c.execute("select 1 from sqlite_master where name='person_photo_history'").fetchone(): return []
            rows = [dict(r) for r in c.execute('select id,captured,last_seen,frame,snapshot,box,basis,evidence,status,event_ids from person_photo_history where subject_id=? and object_id=?'+(" and status='confirmed'" if confirmed_only else '')+' order by captured desc limit 200',(subject_id,object_id))]
        for row in rows:
            for key in ('box','evidence','event_ids'): row[key]=json.loads(row[key])
        return rows

    def image(self, subject_id, object_id, photo_id):
        if not self.path.is_file(): return None
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3) as c:
            if not c.execute("select 1 from sqlite_master where name='person_photo_history'").fetchone(): return None
            row=c.execute('select image from person_photo_history where subject_id=? and object_id=? and id=?',(subject_id,object_id,photo_id)).fetchone()
        return bytes(row[0]) if row else None
