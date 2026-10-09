from io import BytesIO
import sqlite3

import pytest

from jshi.recognition.photos import PersonPhotos


def image_bytes(color):
    from PIL import Image
    out = BytesIO()
    Image.new('RGB', (80, 40), color).save(out, 'PNG')
    return out.getvalue()


def test_photo_history_keeps_periods_and_isolates_candidates_and_people(tmp_path):
    store = PersonPhotos(tmp_path)
    first = store.put('subject', 'lux', image_bytes('red'), captured=100)
    latest = store.put('subject', 'lux', image_bytes('blue'), captured=200)
    candidate = store.put('subject', 'lux', image_bytes('green'), captured=300,
                          status='candidate', event_ids=('event-1',))
    assert store.get('subject', 'lux') == store.image('subject', 'lux', latest)
    assert store.image('subject', 'other', first) is None
    assert store.image('other', 'lux', first) is None
    assert [r['id'] for r in store.list('subject', 'lux')] == [candidate, latest, first]
    assert len(store.list('subject', 'lux', confirmed_only=True)) == 2
    store.put('subject', 'lux', image_bytes('green'), captured=400,
              status='confirmed', event_ids=('event-2',))
    rows = store.list('subject', 'lux')
    assert len(rows) == 3
    green = next(r for r in rows if r['id'] == candidate)
    assert green['captured'] == 300 and green['last_seen'] == 400
    assert green['event_ids'] == ['event-1', 'event-2']
    assert store.get('subject', 'lux') == store.image('subject', 'lux', candidate)


def test_photo_crop_and_legacy_migration(tmp_path):
    from PIL import Image
    store = PersonPhotos(tmp_path)
    old = image_bytes('red')
    with sqlite3.connect(store.path) as c:
        c.execute('create table person_photos(subject_id text, object_id text, image blob, updated real, primary key(subject_id,object_id))')
        c.execute('insert into person_photos values(?,?,?,?)', ('subject', 'lux', old, 100))
    assert store.get('subject', 'lux') == old
    new = store.put('subject', 'lux', image_bytes('blue'), captured=200,
                    box=(0.5, 0, 1, 1), frame='frame-1', snapshot='snapshot-1')
    assert len(store.list('subject', 'lux')) == 2
    with Image.open(BytesIO(store.image('subject', 'lux', new))) as image:
        assert image.size == (40, 40)
    row = store.list('subject', 'lux')[0]
    assert row['frame'] == 'frame-1' and row['snapshot'] == 'snapshot-1'
    with pytest.raises(ValueError):
        store.put('subject', 'lux', old, box=(0, 0, 2, 1))
    with pytest.raises(ValueError):
        store.put('subject', 'lux', old, captured=float('nan'))
