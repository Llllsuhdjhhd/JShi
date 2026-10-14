"""Optional local face embeddings, anonymous enrollment and explicit identity links."""
from pathlib import Path
from hashlib import sha256
import json
import sqlite3
from threading import RLock
from time import time


def cosine(a, b):
    import math
    denominator = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x,y in zip(a,b)) / denominator if denominator else -1


class FaceReferences:
    def __init__(self, root, config):
        self.path = Path(root) / 'face_references.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.lock = RLock()
        with sqlite3.connect(self.path) as c:
            c.executescript('''
                create table if not exists faces(subject text, face text, object_id text not null default '',
                    basis text not null default '', primary key(subject,face));
                create table if not exists samples(subject text, face text, hash text, image blob,
                    feature text, observed real, frame text not null default '', primary key(subject,face,hash));
            ''')
            if 'frame' not in {r[1] for r in c.execute('pragma table_info(samples)')}:
                c.execute("alter table samples add column frame text not null default ''")

    def match_or_enroll(self, subject, feature, image, *, observed_at=None, frame='', collect=True):
        with self.lock, sqlite3.connect(self.path) as c:
            rows = c.execute('select f.face,f.object_id,s.feature from faces f join samples s on s.subject=f.subject and s.face=f.face where f.subject=?', (subject,)).fetchall()
            scores = {}
            identities = {}
            for face, identity, embedding in rows:
                scores[face] = max(scores.get(face, -1), cosine(feature, json.loads(embedding)))
                identities[face] = identity
            ranked = sorted(scores, key=lambda k:scores[k], reverse=True)
            match = ranked[0] if ranked else None
            score = scores[match] if match else None
            gap = score - scores[ranked[1]] if len(ranked) > 1 else None
            reliable = match is not None and score >= self.config.face_match_threshold and (gap is None or gap >= self.config.face_match_gap)
            if not reliable:
                # Ambiguous close matches must not seed another identity or contaminate one.
                if match is not None and score >= self.config.face_match_threshold:
                    return {'face':match, 'object_id':identities[match], 'certainty':'tentative', 'score':round(score,3), 'gap':round(gap,3)}
                if not collect:
                    return {'face':'','object_id':'','certainty':'unknown','reference_collection':'disabled'}
                number = c.execute('select count(*) from faces where subject=?', (subject,)).fetchone()[0] + 1
                if number > self.config.face_identity_limit:
                    return {'face':'', 'object_id':'', 'certainty':'unknown', 'reference_collection':'capacity_reached'}
                match = f'F{number}'
                c.execute('insert into faces(subject,face) values(?,?)', (subject,match))
                identities[match] = ''
            count = c.execute('select count(*) from samples where subject=? and face=?', (subject,match)).fetchone()[0]
            # Automatic anonymous reference collection is bounded and can be disabled.
            if collect and count < self.config.portrait_limit and (count == 0 or score is None or score < self.config.portrait_similarity_limit):
                c.execute('insert or ignore into samples(subject,face,hash,image,feature,observed,frame) values(?,?,?,?,?,?,?)',
                          (subject,match,sha256(image).hexdigest(),image,json.dumps(feature),observed_at if observed_at is not None else time(),frame))
                if identities.get(match):
                    from jshi.recognition.photos import PersonPhotos
                    PersonPhotos(self.path.parent).put(subject, identities[match], image,
                        captured=observed_at, frame=frame, basis='local_face_match',
                        evidence=(f'视觉面孔关联 {match}',), status='candidate')
            return {'face':match, 'object_id':identities.get(match,''), 'certainty':'tentative' if identities.get(match) else 'unknown',
                    'score':round(score,3) if score is not None else None, 'gap':round(gap,3) if gap is not None else None}

    def bind(self, subject, face, object_id, *, basis):
        if not object_id or basis not in ('main_visual_association', 'manual'):
            raise ValueError('explicit visual identity association required')
        with self.lock, sqlite3.connect(self.path) as c:
            row = c.execute('select object_id from faces where subject=? and face=?', (subject,face)).fetchone()
            if row is None:
                raise KeyError('face unavailable')
            if row[0] and row[0] != object_id and basis != 'manual':
                raise ValueError('existing identity conflict requires manual correction')
            c.execute('update faces set object_id=?,basis=? where subject=? and face=?', (object_id,basis,subject,face))
            photos = c.execute('select image,observed,frame from samples where subject=? and face=?', (subject,face)).fetchall()
        from jshi.recognition.photos import PersonPhotos
        archive = PersonPhotos(self.path.parent)
        if row[0] and row[0] != object_id:
            archive.withdraw_face_association(subject, row[0], face)
        for photo, at, frame in photos:
            archive.put(subject, object_id, photo, captured=at, frame=frame, basis=basis,
                        evidence=(f'视觉面孔关联 {face}',), status='confirmed')


class LocalFaces:
    def __init__(self, root, config, extractor=None):
        self.config = config
        self.root = Path(root)
        self.references = None
        self.extractor = extractor
        self.detector = self.recognizer = None

    def _extract(self, image):
        import cv2
        import numpy as np
        if self.detector is None:
            self.detector = cv2.FaceDetectorYN.create(self.config.face_detector_model, '', (320,320), .9)
            self.recognizer = cv2.FaceRecognizerSF.create(self.config.face_recognizer_model, '')
        decoded = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
        height, width = decoded.shape[:2]
        self.detector.setInputSize((width,height))
        _, faces = self.detector.detect(decoded)
        found = []
        for face in faces if faces is not None else ():
            x,y,w,h = face[:4]
            if min(w,h) < self.config.face_min_size:
                continue
            aligned = self.recognizer.alignCrop(decoded, face)
            feature = self.recognizer.feature(aligned).flatten().tolist()
            _, encoded = cv2.imencode('.jpg', aligned)
            found.append({'box':[round(max(0,min(1,v)),3) for v in (x/width,y/height,(x+w)/width,(y+h)/height)],
                          'feature':feature, 'image':encoded.tobytes()})
        return found

    def analyze(self, image, *, subject, observed_at=None, frame=''):
        if not self.config.face_enabled:
            return {'_status':'not_run', 'reason':'disabled', 'faces':[]}
        models = (self.config.face_detector_model, self.config.face_recognizer_model)
        if self.extractor is None and not all(p and Path(p).is_file() for p in models):
            return {'_status':'not_run', 'reason':'missing_models', 'faces':[]}
        found = (self.extractor or self._extract)(image)
        result = []
        for face in found:
            if self.config.portrait_enabled or (self.root/'face_references.sqlite3').is_file():
                if self.references is None:
                    self.references = FaceReferences(self.root, self.config)
                match = self.references.match_or_enroll(subject, face['feature'], face['image'], observed_at=observed_at, frame=frame,
                                                       collect=self.config.portrait_enabled)
            else:
                match = {'face':'', 'object_id':'', 'certainty':'unknown', 'reference_collection':'disabled'}
            result.append({**match, 'box':face['box']})
        return {'faces':result}

    def bind(self, subject, face, object_id, *, basis):
        if not self.config.portrait_enabled or self.references is None:
            raise ValueError('automatic face references disabled or unavailable')
        return self.references.bind(subject, face, object_id, basis=basis)
