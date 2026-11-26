"""Frame-bound visual derivatives and time-bounded evidence for scene context."""
from io import BytesIO
import json
import math
from hashlib import sha256



def encoded_view(image_data, box=None, *, side=512):
    from PIL import Image
    with Image.open(BytesIO(image_data)) as image:
        if box is None:
            if max(image.size) <= side:
                return image_data
            image.thumbnail((side, side))
        else:
            if len(box) != 4 or not all(math.isfinite(v) and 0 <= v <= 1 for v in box):
                raise ValueError('焦点区域超出图片')
            x, y, right, bottom = box
            if x >= right or y >= bottom:
                raise ValueError('焦点区域为空')
            w, h = image.size
            image = image.crop((int(x*w), int(y*h), math.ceil(right*w), math.ceil(bottom*h)))
        output = BytesIO()
        image.save(output, 'JPEG', quality=88 if box else 72)
        return output.getvalue()


def retain_view(c, subject, frame, target, box, config):
    region = json.dumps(box, separators=(',', ':'))
    key = (subject, frame, target, region)
    if c.execute('select 1 from visual_views where subject=? and frame=? and target=? and region=?', key).fetchone():
        return True
    row = c.execute('select a.data from frames f join assets a on f.asset=a.id where f.subject=? and f.id=?',
                    (subject, frame)).fetchone()
    if not row:
        return False
    data = encoded_view(row[0], box, side=config.overview_side)
    asset = sha256(data).hexdigest()
    # Account for pre-migration photos that still occupy their full size.
    used = c.execute('''select coalesce(sum(size),0) from assets where id!=? and id in (
        select asset from visual_views union select f.asset from frames f join snapshots s
        on s.description_frame=f.id where s.id in (select snapshot from bindings where retain_photo=1
        union select snapshot from events where retain_photo=1) and f.id!=?
        and not exists(select 1 from visual_views v where v.frame=f.id and v.target=''))''',
                     (asset,frame)).fetchone()[0]
    if used + len(data) > config.memory_bytes:
        return False
    c.execute('insert or ignore into assets values(?,?,?)', (asset, data, len(data)))
    c.execute('insert into visual_views values(?,?,?,?,?)', (*key, asset))
    return True


def scene_history(store, subject, frame):
    """Prior independently described frames from the same physical capture."""
    with store.db() as c:
        rows = c.execute("""
            select s.frame, s.captured, s.description, (select data from evidence where frame=f.id and module='face') as faces from frames anchor
            join frames f on f.subject=anchor.subject and f.source=anchor.source
                and f.capture_id=anchor.capture_id and f.captured < anchor.captured
                and f.captured >= anchor.captured-?
            join snapshots s on s.subject=f.subject and s.frame=f.id
            where anchor.subject=? and anchor.id=? and s.frame=s.description_frame
                and s.described_at>0 and s.reason not in ('observation_result','recall_image')
            group by f.id order by f.captured desc limit 2""", (store.config.recent_scene_seconds, subject, frame))
        return [dict(row) for row in rows]

def layout_signature(detections):
    """Coarse positions of visible fixtures; keep people and portable objects out."""
    fixtures = {'chair', 'couch', 'bed', 'dining table', 'toilet', 'sink', 'refrigerator',
                'oven', 'bookcase', 'bookshelf', 'door', 'window', 'desk'}
    cells = set()
    for item in detections:
        label = item.get('class')
        box = item.get('box')
        if label in fixtures and isinstance(box, (list,tuple)) and len(box)==4:
            if all(isinstance(v,(int,float)) and math.isfinite(v) and 0<=v<=1 for v in box):
                center = (box[0]+box[2])/2
                cells.add(label+'@'+('左' if center<1/3 else '右' if center>2/3 else '中'))
    return sorted(cells)


def place_experience(c, subject, frame, detections):
    """Use the most recent attributed location in this view, up to a scene break."""
    anchor = c.execute("""select p.place, p.captured from frames current
        join place_facts p on p.subject=current.subject and p.capture=current.capture_id
        and p.source=current.source and p.captured<=current.captured
        where current.subject=? and current.id=? and p.capture!=''
        and not exists(select 1 from snapshots change join frames f on f.id=change.frame
            where f.subject=current.subject and f.capture_id=current.capture_id
            and f.source=current.source and change.change_degree='high'
            and f.captured>p.captured and f.captured<=current.captured)
        order by p.captured desc,p.rowid desc limit 1""", (subject,frame)).fetchone()
    if not anchor:
        cells = set(layout_signature(detections))
        if len(cells)>=2:
            # Count independent named visits, never additional frames of the same visit.
            matches = {}
            for row in c.execute("""select p.place,date(p.captured,'unixepoch','localtime') as day,p.layout from place_facts p join frames f
                    on f.id=? and f.subject=p.subject where p.subject=? and p.layout!='[]'
                    and p.captured<f.captured and p.capture!=f.capture_id and p.capture!=''""", (frame,subject)):
                saved = set(json.loads(row['layout']))
                if len(cells & saved)>=2 and len(cells & saved)/len(cells | saved)>=0.75:
                    matches.setdefault(row['place'],set()).add(row['day'])
            choices = [place for place,visits in matches.items() if len(visits)>=2]
            if len(choices)==1 and len(matches)==1:
                return {'status':'candidate','place':choices[0],'references':[],
                        'layout':sorted(cells),'visits':len(matches[choices[0]])}
        return {'status':'unknown','references':[]}
    rows = c.execute("""select p.feature,p.observation,p.captured,p.place from place_facts p
        join frames current on current.id=? and current.subject=p.subject
        where p.subject=? and p.place=? and p.captured<=current.captured
        order by p.captured desc,p.rowid desc""", (frame,subject,anchor['place']))
    latest = {}
    for row in rows:
        latest.setdefault(row['feature'],dict(row))
        if len(latest)==4:
            break
    return {'status':'reported','references':list(latest.values())}

def participant_context(recent, snapshot, object_codes):
    """Report the source of presence evidence without refreshing an older sighting."""
    codes = {object_id:code for code,object_id in object_codes}
    def people(evidence):
        if not evidence or evidence.get('status')!='completed':
            return set()
        return {codes[f['object_id']] for f in (evidence.get('result') or {}).get('faces',())
                if f.get('object_id') in codes}
    now = set()
    for item in snapshot.get('analysis',()):
        if item.get('module')=='face':
            now.update(people(item))
    previous = set()
    for row in recent:
        if row['faces']:
            previous.update(people(json.loads(row['faces'])))
    notes = []
    if now:
        notes.append('当前图中的已关联面孔：'+'、'.join(sorted(now)))
    if previous-now:
        notes.append('先前画面关联过、当前在场情况待核实：'+'、'.join(sorted(previous-now)))
    return notes
