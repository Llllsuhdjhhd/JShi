"""Explicitly bound person photos, separate from transient camera samples."""
from io import BytesIO
from pathlib import Path
import sqlite3
from time import time


class PersonPhotos:
    def __init__(self, root):
        self.path = Path(root) / 'person_photos.sqlite3'

    def get(self, subject_id, object_id):
        if not self.path.is_file():
            return None
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3) as c:
            row = c.execute('select image from person_photos where subject_id=? and object_id=?',
                            (subject_id, object_id)).fetchone()
        return bytes(row[0]) if row else None

    def put(self, subject_id, object_id, data):
        from PIL import Image, ImageOps
        if not data or len(data) > 8_000_000:
            raise ValueError('人物照片须小于 8 MB')
        with Image.open(BytesIO(data)) as original:
            if original.width * original.height > 24_000_000:
                raise ValueError('人物照片尺寸过大')
            image = ImageOps.exif_transpose(original).convert('RGB')
            image.thumbnail((1200, 1200))
            out = BytesIO()
            image.save(out, 'JPEG', quality=85)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=3) as c:
            c.execute('create table if not exists person_photos(subject_id text, object_id text, image blob not null, updated real not null, primary key(subject_id,object_id))')
            c.execute('insert or replace into person_photos values(?,?,?,?)',
                      (subject_id, object_id, out.getvalue(), time()))
