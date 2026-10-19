"""Personal classifiers: bounded background collection/training and immutable publication."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import BoundedSemaphore, RLock
from time import time
from uuid import uuid4

import numpy as np

from .personal_model import atomic_json, clip_scores, duration_bucket, load_weights, normalize, pack_weights, split_pcm
from .personal_store import OTHER_VOICE, PersonalStore


LOG = logging.getLogger(__name__)


class PersonalSpeakers:
    def __init__(self, root, profiles):
        self.store = PersonalStore(root)
        self.root, self.profiles = Path(root), profiles
        self.lock = RLock()
        self.collector = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jshi-personal-collect')
        self.trainer = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jshi-personal-train')
        self.inference = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jshi-personal-predict')
        self.capacity = BoundedSemaphore(64)
        self.training = set()
        self.cache = {}
        self.packed = {}
        self.closed = False
        self.predict_future = None
        self.child = None
        self.last_problem = ''

    def _person(self, oid):
        person = self.profiles.get(oid)
        if not person or person.status == 'rejected' or person.source in {'voice_anonymous', 'voice_scene'}:
            raise ValueError('请选择已建立档案的具体人物')
        return person

    def select_targets(self, object_ids, *, source='manual', pinned=True):
        if not isinstance(object_ids, list):
            raise ValueError('人物列表格式错误')
        for oid in object_ids:
            self._person(oid)
        return self.store.select(object_ids, source=source, pinned=pinned)

    def set_target(self, oid, *, selected=True, pinned=True):
        self._person(oid)
        if type(selected) is not bool or type(pinned) is not bool:
            raise ValueError('人物选择状态须为布尔值')
        with self.store.connect() as db:
            exists = db.execute('SELECT 1 FROM targets WHERE object_id=?', (oid,)).fetchone()
            if not selected:
                db.execute('DELETE FROM targets WHERE object_id=?', (oid,))
            else:
                if not exists and db.execute('SELECT COUNT(*) FROM targets').fetchone()[0] >= self.store.value(db, 'settings')['budget']:
                    raise ValueError('目标人物已经达到预算上限')
                db.execute('''INSERT INTO targets(object_id,pinned,source) VALUES(?,?,'manual')
                    ON CONFLICT(object_id) DO UPDATE SET pinned=excluded.pinned,source='manual' ''', (oid, int(pinned)))

    def _directory(self, oid):
        path = self.root / 'models' / hashlib.sha256(oid.encode()).hexdigest()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def manifest(self, oid):
        path = self._directory(oid) / 'versions.json'
        if not path.exists():
            return {'active': None, 'previous': None}
        value = json.loads(path.read_text(encoding='utf-8'))
        for key in ('active', 'previous'):
            item = value.get(key)
            if item and (not re.fullmatch('[0-9a-f]{32}', item['version']) or item['object_id'] != oid):
                raise ValueError('个人模型版本记录无效')
        return value

    def weights(self, oid, entry):
        key = (oid, entry['version'])
        with self.lock:
            if key not in self.cache:
                self.cache[key] = load_weights(self._directory(oid) / (entry['version'] + '.npz'), entry['dimension'])
            return self.cache[key]

    def status(self):
        _, epoch = self.store.revision()
        targets = []
        for row in self.store.targets():
            try:
                person = self.profiles.get(row['object_id'])
                versions = self.manifest(row['object_id'])
                active = versions['active']
                valid = bool(active and person and person.status != 'rejected')
                targets.append({**row, 'label': person.label if person else row['object_id'],
                                'reference_only': bool(active and active['label_epoch'] != epoch),
                                'available': valid, 'active': active, 'previous': versions['previous'],
                                'progress': self.dataset_progress(row['object_id'])})
            except (ValueError, OSError, KeyError) as exc:
                targets.append({**row, 'available': False, 'detail': str(exc)})
        return {'settings': self.store.settings(), 'targets': targets, 'counts': self.store.counts(),
                'background': self.background_summary(), 'last_problem': self.last_problem}

    def labelled_samples(self, oid, *, audio=False):
        people = {p.object_id: p for p in self.profiles.list()}
        rows = []
        for row in self.store.samples(audio=audio):
            if row['object_id'] == OTHER_VOICE:
                rows.append({**row, 'label': 0, 'background': False, 'other': True})
                continue
            person = people.get(row['object_id'])
            if not person or person.status == 'rejected' or person.source in {'voice_anonymous', 'voice_scene'}:
                continue
            positive = row['object_id'] == oid
            # Another person's tentative/automatic label must never become a negative.
            if not positive and row['source'] != 'manual':
                continue
            rows.append({**row, 'label': int(positive), 'background': False})
        rows.extend({**r, 'source': 'public', 'label': 0, 'background': True}
                    for r in self.store.background_samples(audio=audio))
        for row in rows:
            row['device_negative'] = not row['label'] and (
                row.get('other', False) or row.get('dataset') in {'voxconverse', 'musan-broadcast'})
            row['local_device_negative'] = row['device_negative'] and row['source'] == 'manual'
        return rows

    def background_summary(self):
        rows = self.store.background_samples()
        return {'clips': len(rows), 'speakers': len({(r['dataset'], r['speaker']) for r in rows}),
                'datasets': sorted({r['dataset'] for r in rows})}

    def dataset_progress(self, oid):
        from .personal_train import readiness, split_plan
        rows = self.labelled_samples(oid)
        config = self.store.settings()
        ready, counts = readiness(rows, config)
        minimum = {name: {kind: config['min_train_'+kind] if name == 'train' else config['min_eval_'+kind]
                          for kind in ('positive', 'negative')}
                   for name in ('train', 'calibration', 'validation')}
        positive = [r for r in rows if r['label']]
        return {'positive': len(positive), 'positive_sessions': len({r['session_id'] for r in positive}),
                'negative_manual': sum(not r['label'] and r['source'] == 'manual' for r in rows),
                'negative_public': sum(r['source'] == 'public' for r in rows),
                'negative_device': sum(r['device_negative'] for r in rows),
                'negative_device_local': sum(r['local_device_negative'] for r in rows),
                'negative_device_public': sum(r['device_negative'] and not r['local_device_negative'] for r in rows),
                'counts': counts, 'minimum': minimum, 'enough_saved_records': ready,
                'validation_mode': split_plan(rows)[1]}

    def make_features(self, pcm, speakers):
        if speakers is None or getattr(speakers, 'extractor', None) is None:
            raise ValueError('声音特征模型尚未加载，原始训练录音已保留')
        pieces = split_pcm(pcm)
        vectors, durations = [], []
        for piece in pieces:
            values = np.frombuffer(piece, dtype='<i2').astype('float32') / 32768
            # Use the extractor directly: preserve the original embedding() length policy.
            with speakers.lock:
                stream = speakers.extractor.create_stream()
                stream.accept_waveform(sample_rate=16000, waveform=values)
                stream.input_finished()
                if not speakers.extractor.is_ready(stream):
                    raise ValueError('当前特征模型不能处理这段真实长度的声音；没有重复补长')
                vector = normalize(speakers.extractor.compute(stream))
            vectors.append(vector.tolist())
            durations.append(len(piece) / 32000)
        if len({len(v) for v in vectors}) != 1:
            raise ValueError('特征维度不一致')
        return vectors, durations

    def capture(self, pcm, oid, session_id, input_id, speakers, *, source, score=None, overlap=False):
        if oid != OTHER_VOICE or source != 'manual':
            self._person(oid)
        sid = self.store.save(pcm, oid, session_id, input_id, source=source, score=score, overlap=overlap)
        if sid is None:
            return None
        model_id = getattr(speakers, 'model_id', '')
        try:
            if model_id and not self.store.features(sid, model_id):
                vectors, durations = self.make_features(pcm, speakers)
                self.store.cache(sid, model_id, vectors, durations)
            if model_id:
                self.last_problem = ''
        except (ValueError, RuntimeError, OSError) as exc:
            self.last_problem = str(exc)
            LOG.warning('Personal features deferred: %s', exc)
        for target in self.store.targets():
            self.request_training(target['object_id'], speakers)
        return sid

    def submit_capture(self, *args, **kwargs):
        if self.closed or not self.capacity.acquire(blocking=False):
            self.last_problem = '个人声音采集队列已满，未保存本次新增样本'
            return None
        def work():
            try:
                return self.capture(*args, **kwargs)
            except Exception as exc:
                self.last_problem = str(exc)
                LOG.exception('Personal sample collection failed')
                raise
            finally:
                self.capacity.release()
        future = self.collector.submit(work)
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        return future

    def collect_original(self, pcm, transcript, evidence, session_id, speakers, *, candidates=()):
        """Collect from actual original-encoder scores, independently of JEV guesses."""
        config = self.store.settings()
        if not config['enabled'] or not pcm or transcript.overlap:
            return None
        try:
            note = json.loads(transcript.identity_note or '{}')
            note = note if isinstance(note, dict) else {}
        except (ValueError, TypeError):
            note = {}
        # Prefer the original full ranking over the shortened already-recognized list.
        ranked = note.get('candidates') or candidates
        valid = {}
        for row in ranked:
            if (not isinstance(row, dict) or row.get('source', 'voiceprint') not in {'voiceprint', 'voiceprint_match'}
                    or type(row.get('score')) not in (int, float) or not math.isfinite(row['score'])):
                continue
            try:
                person = self._person(row.get('object_id'))
            except ValueError:
                continue
            oid = person.object_id
            if oid not in valid or row['score'] > valid[oid]['score']:
                valid[oid] = {'object_id': oid, 'label': person.label, 'score': row['score']}
        rows = sorted(valid.values(), key=lambda row: row['score'], reverse=True)
        if rows:
            best = rows[0]
            # A single ranked item must be the only competitor, or have a runner-up
            # score in the original match report; a truncated list proves no gap.
            runner = rows[1]['score'] if len(rows) > 1 else note.get('runner_up')
            if runner is None and len(rows) == 1:
                known = [p for p in self.profiles.list() if p.status != 'rejected'
                         and p.source not in {'voice_anonymous', 'voice_scene'}]
                if len(known) > 1:
                    return None
        elif (evidence.method == 'voiceprint_match' and evidence.status == 'recognized'
              and not transcript.identity_uncertain and not transcript.identity_tentative):
            try:
                person = self._person(evidence.object_id)
            except ValueError:
                return None
            best = {'object_id': person.object_id, 'label': person.label, 'score': transcript.confidence}
            runner = note.get('runner_up')
            if runner is None and len([p for p in self.profiles.list() if p.status != 'rejected'
                                      and p.source not in {'voice_anonymous', 'voice_scene'}]) > 1:
                return None
        else:
            return None
        score = best['score']
        try:
            current = note.get('collection', {}).get('current_score')
            if current is not None:
                score = min(score, float(current)) if score is not None else float(current)
        except (ValueError, TypeError, AttributeError):
            return None
        if type(score) not in (int, float) or not math.isfinite(score) or not config['auto_score'] <= score <= 1:
            return None
        if runner is not None and (type(runner) not in (int, float) or not math.isfinite(runner)
                                   or score - runner + 1e-9 < config['auto_margin']):
            return None
        future = self.submit_capture(pcm, best['object_id'], session_id, transcript.input_id, speakers,
                                     source='voiceprint', score=score)
        return {**best, 'score': score, 'future': future} if future is not None else None

    def _records(self, oid, speakers):
        revision, epoch = self.store.revision()
        model_id = getattr(speakers, 'model_id', '')
        if not model_id:
            raise ValueError('请先加载用于训练的声音特征模型')
        records, dimension = [], None
        for row in self.labelled_samples(oid, audio=True):
            if row['revision'] > revision:
                continue
            features = self.store.features(row['id'], model_id, background=row['background'])
            if features is None:
                try:
                    vectors, durations = self.make_features(row['pcm'], speakers)
                    self.store.cache(row['id'], model_id, vectors, durations, background=row['background'])
                    features = {'vectors': vectors, 'durations': durations, 'dimension': len(vectors[0])}
                except (ValueError, RuntimeError):
                    continue
            if dimension is None:
                dimension = features['dimension']
            if dimension != features['dimension']:
                raise ValueError('同一特征模型产生不同维度，停止训练')
            records.append({'id': row['id'], 'session_id': row['session_id'], 'source': row['source'],
                            'label': row['label'], 'device_negative':row['device_negative'],
                            'local_device_negative':row['local_device_negative'], **features})
        return {'object_id': oid, 'model_id': model_id, 'dimension': dimension,
                'revision': revision, 'label_epoch': epoch, 'config': self.store.settings(), 'records': records}

    def request_training(self, oid, speakers, *, force=False):
        target = next((r for r in self.store.targets() if r['object_id'] == oid), None)
        if not target:
            raise ValueError('请先将此人加入训练目标')
        if self.closed or not self.store.settings()['enabled']:
            return False
        revision, _ = self.store.revision()
        # Count original recordings, not three segments of a single recording.
        new_count = sum(r['revision'] > target['attempted_revision'] for r in self.labelled_samples(oid))
        if not force and new_count < self.store.settings()['batch_samples']:
            return False
        with self.lock:
            if oid in self.training:
                return False
            self.training.add(oid)
        self.store.state(oid, 'queued', '等待后台训练')
        self.trainer.submit(self._train, oid, speakers)
        return True

    def _train(self, oid, speakers):
        job = self.root / 'jobs' / uuid4().hex
        job.mkdir(parents=True, exist_ok=True)
        snapshot = None
        snapshot_file, output, result_file = job/'snapshot.json', job/'candidate.npz', job/'result.json'
        try:
            if self.closed:
                return
            self.store.state(oid, 'preparing', '准备训练、校准和独立验证数据')
            snapshot = self._records(oid, speakers)
            from .personal_train import readiness
            ready, counts = readiness(snapshot['records'], snapshot['config'])
            self.store.state(oid, 'training' if ready else 'collecting',
                             '样本分组计数：' + json.dumps(counts, ensure_ascii=False),
                             attempted_revision=snapshot['revision'])
            if not ready:
                return
            previous = self.manifest(oid)['active']
            snapshot['base_version'] = previous['version'] if previous else None
            if (previous and previous['model_id'] == snapshot['model_id']
                    and previous['dimension'] == snapshot['dimension']):
                snapshot['previous'] = {'path': str(self._directory(oid)/(previous['version']+'.npz')),
                    'threshold': previous['threshold'],
                    'verified': previous.get('verified', True) and previous['label_epoch'] == snapshot['label_epoch']}
            atomic_json(snapshot_file, snapshot)
            env = {**os.environ, 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
            with (job/'worker.log').open('w', encoding='utf-8') as log:
                with self.lock:
                    if self.closed:
                        return
                    self.child = subprocess.Popen([sys.executable, '-m', 'jshi.voice.personal_train',
                        '--snapshot', str(snapshot_file), '--output', str(output), '--result', str(result_file)],
                        env=env, stdout=log, stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    child = self.child
                try:
                    child.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    raise ValueError('后台训练超过五分钟，本次不发布')
                finally:
                    with self.lock:
                        self.child = None
            if child.returncode or not result_file.exists():
                raise ValueError('后台训练未完成；请检查训练依赖和 worker.log')
            result = json.loads(result_file.read_text(encoding='utf-8'))
            if result['status'] != 'ready':
                self.store.state(oid, result['status'], result.get('detail', '未发布'))
                return
            self.publish(oid, snapshot, output, result)
            self.store.state(oid, 'ready', result.get('detail', '已训练并发布')+'；当前及上一版本可查看或回退')
        except Exception as exc:
            self.last_problem = str(exc)
            self.store.state(oid, 'error', str(exc))
            LOG.exception('Personal classifier training failed')
        finally:
            with self.lock:
                self.training.discard(oid)
            # Delete only named temporary artifacts created by this job.
            for path in (snapshot_file, output, result_file):
                path.unlink(missing_ok=True)
            if snapshot and not self.closed and snapshot['label_epoch'] != self.store.revision()[1]:
                try:
                    self.request_training(oid, speakers, force=True)
                except ValueError:
                    pass  # Target removed while this candidate was training.

    def publish(self, oid, snapshot, output, result):
        weights = load_weights(output, snapshot['dimension'])
        with self.lock, self.store.connect() as db:
            if self.closed or snapshot['label_epoch'] != self.store.value(db, 'label_epoch'):
                raise ValueError('训练期间标签已修正，候选模型作废')
            if not db.execute('SELECT 1 FROM targets WHERE object_id=?', (oid,)).fetchone():
                raise ValueError('训练期间目标已移除，不发布')
            if not self.store.value(db, 'settings')['enabled']:
                raise ValueError('个人声音学习已暂停，不发布候选版本')
            self._person(oid)
            version = uuid4().hex
            entry = {key: snapshot[key] for key in ('object_id', 'model_id', 'dimension', 'revision', 'label_epoch')}
            entry.update({key: result[key] for key in ('threshold', 'validation', 'calibration', 'counts', 'duration_reports')})
            entry.update({key: result[key] for key in ('verified', 'validation_mode', 'detail') if key in result})
            entry.update(version=version, created=time())
            entry.update(classifier_mode='independent_binary', architecture='mlp-256-256-128-v1')
            old = self.manifest(oid)
            if ('base_version' in snapshot
                    and snapshot['base_version'] != (old['active']['version'] if old['active'] else None)):
                raise ValueError('训练期间已经切换或回退版本，本候选作废')
            previous = old['active']
            destination = self._directory(oid) / (version + '.npz')
            output.replace(destination)
            atomic_json(self._directory(oid)/'versions.json', {'active': entry, 'previous': previous})
            self.cache[(oid, version)] = weights
            self._prune(oid, {version, previous['version'] if previous else ''})

    def _prune(self, oid, keep):
        for path in self._directory(oid).glob('*.npz'):
            if re.fullmatch('[0-9a-f]{32}', path.stem) and path.stem not in keep:
                path.unlink(missing_ok=True)
        self.cache = {key: value for key, value in self.cache.items() if key[0] != oid or key[1] in keep}

    def rollback(self, oid):
        with self.lock, self.store.connect() as db:
            versions = self.manifest(oid)
            previous = versions['previous']
            if not previous:
                raise ValueError('没有可回退的备份版本')
            self.weights(oid, previous)
            atomic_json(self._directory(oid)/'versions.json', {'active': previous, 'previous': versions['active']})
        return previous

    def predict(self, pcm, speakers):
        from .identity_comparison import MLP_SCORE_FLOOR
        config = self.store.settings()
        if not config['enabled'] or not pcm:
            return {'status': 'disabled', 'matched': '', 'ranking': []}
        model_id = getattr(speakers, 'model_id', '')
        _, epoch = self.store.revision()
        bucket = duration_bucket(len(pcm) / 32000)
        models = []
        with self.lock:
            for target in self.store.targets():
                person = self.profiles.get(target['object_id'])
                if not person or person.status == 'rejected':
                    continue
                entry = self.manifest(person.object_id)['active']
                if entry and entry['model_id'] == model_id:
                    models.append((person, {**entry, 'stale_labels':entry['label_epoch'] != epoch},
                        self.weights(person.object_id, entry)))
        if not models:
            return {'status': 'no_ready_models', 'matched': '', 'ranking': []}
        vectors, durations = self.make_features(pcm, speakers)
        if any(entry['dimension'] != len(vectors[0]) for _, entry, _ in models):
            raise ValueError('当前音频特征与个人模型维度不一致')
        key = tuple((person.object_id, entry['version']) for person, entry, _ in models)
        with self.lock:
            if key not in self.packed:
                self.packed[key] = pack_weights([w for _, _, w in models])
                while len(self.packed) > 3:
                    self.packed.pop(next(iter(self.packed)))
            packed = self.packed[key]
        scores = clip_scores(vectors, durations, [w for _, _, w in models], packed=packed)
        ranking = sorted([{'object_id': person.object_id, 'label': person.label, 'score': float(score),
                           'stale_labels':entry['stale_labels'],
                           'verified': not entry['stale_labels'] and entry.get('verified', True) and entry.get('duration_reports', {}).get(bucket, {}).get('enabled', False),
                           'identity_enabled': not entry['stale_labels'] and entry.get('duration_reports', {}).get(bucket, {}).get('enabled', False)
                               and entry.get('duration_reports', {}).get(bucket, {}).get('identity_enabled', entry.get('verified', True)),
                           'threshold': max(entry['threshold'], MLP_SCORE_FLOOR), 'version': entry['version'], 'source': 'personal_classifier'}
                          for (person, entry, _), score in zip(models, scores)], key=lambda r: r['score'], reverse=True)
        hits = [r for r in ranking if r['verified'] and r['identity_enabled'] and r['score'] >= r['threshold']]
        matched = ''
        if len(hits) == 1 and all(r['threshold']-r['score'] >= config['conflict_margin']
                                 for r in ranking if r['object_id'] != hits[0]['object_id']):
            matched = hits[0]['object_id']
        # A correction during feature extraction invalidates even this in-flight answer.
        with self.lock:
            if self.store.revision()[1] != epoch or not self.store.settings()['enabled']:
                matched = ''
                if self.store.revision()[1] != epoch:
                    for row in ranking:
                        row.update(stale_labels=True, verified=False, identity_enabled=False)
            if matched not in {r['object_id'] for r in self.store.targets()}:
                matched = ''
        return {'status': 'matched' if matched else 'uncertain', 'matched': matched,
                'ranking': ranking, 'segments': len(vectors),
                'debug_features':{'model_id':model_id,'vectors':vectors,'durations':durations}}

    def submit_predict(self, pcm, speakers):
        with self.lock:
            if self.closed or (self.predict_future is not None and not self.predict_future.done()):
                return None
            self.predict_future = self.inference.submit(self.predict, pcm, speakers)
            self.predict_future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            return self.predict_future

    def close(self):
        with self.lock:
            self.closed = True
            if self.child and self.child.poll() is None:
                self.child.terminate()
        self.collector.shutdown(wait=True, cancel_futures=True)
        self.trainer.shutdown(wait=True, cancel_futures=True)
        self.inference.shutdown(wait=True, cancel_futures=True)
