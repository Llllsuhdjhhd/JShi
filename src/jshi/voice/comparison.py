"""A labeled, held-out voice trial. Never enrolls into the live identity bank."""
from __future__ import annotations

import hashlib
import json
import math
import re
import wave
from pathlib import Path
from statistics import median
from threading import RLock
from time import perf_counter
from uuid import uuid4

from .local import LocalSpeakers, cosine


class SpeakerTrial:
    def __init__(self, root: Path, models):
        self.root = root
        self.models = models
        self.lock = RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / 'samples.json'
        self.samples = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else []

    def add(self, label, role, rows, session):
        label = str(label).strip()
        if not label or len(label) > 40 or role not in {'enroll', 'test'}:
            raise ValueError('填写姓名，并选择登记样本或测试样本')
        with self.lock:
            additions = []
            for row in rows:
                pcm, t = row['pcm'], row['transcript']
                seconds = len(pcm or b'') / 32000
                if not pcm or t.overlap or not (.5 <= seconds <= 30):
                    raise ValueError('只可加入 0.5–30 秒、无已知重叠的有效片段')
                if role == 'enroll' and seconds < 5:
                    raise ValueError('对照登记样本每段至少 5 秒，以兼容云端比较')
                digest = hashlib.sha256(pcm).hexdigest()
                for old in (*self.samples, *additions):
                    intersects = old['session'] == session and old['start_ms'] < t.end_ms and t.start_ms < old['end_ms']
                    if old['sha256'] == digest or intersects:
                        raise ValueError('这段录音已加入，或与已选片段重叠；登记与测试须用不同录音')
                additions.append({'id': uuid4().hex, 'label': label, 'role': role, 'sha256': digest,
                    'seconds': seconds, 'session': session, 'start_ms': t.start_ms, 'end_ms': t.end_ms, '_pcm': pcm})
            if len(self.samples) + len(additions) > 64 or sum(s['seconds'] for s in (*self.samples, *additions)) > 600:
                raise ValueError('本组上限 64 段、10 分钟；请开始新一组')
            for item in additions:
                pcm = item.pop('_pcm')
                with wave.open(str(self.root / (item['id'] + '.wav')), 'wb') as out:
                    out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000); out.writeframes(pcm)
            self.samples.extend(additions)
            self._save('samples.json', self.samples)
            return self.summary()

    def _save(self, name, data):
        target = self.root / name
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(target)

    def summary(self):
        return {'samples': [{k: s[k] for k in ('id', 'label', 'role', 'seconds')} for s in self.samples], 'path': str(self.root.resolve())}

    def new_group(self):
        with self.lock:
            if self.samples:
                previous = self.root / 'report.json'
                archive = {'samples': self.samples, 'report': json.loads(previous.read_text(encoding='utf-8')) if previous.is_file() else None}
                self._save('group-' + uuid4().hex + '.json', archive)
            self.samples = []
            self._save('samples.json', [])
            self._save('report.json', {'status': 'not_run', 'reason': '新一组尚未运行'})
            return self.summary()

    def run(self):
        import numpy as np
        with self.lock:
            if not any(s['role'] == 'enroll' for s in self.samples) or not any(s['role'] == 'test' for s in self.samples):
                raise ValueError('先加入登记样本和另一次发言的测试样本')
            decoded = []
            for s in self.samples:
                if not re.fullmatch('[0-9a-f]{32}', s['id']):
                    raise ValueError('无效的样本编号')
                with wave.open(str(self.root / (s['id'] + '.wav')), 'rb') as audio:
                    if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                        raise ValueError('样本须为 16kHz 单声道 PCM16')
                    pcm = audio.readframes(audio.getnframes())
                if hashlib.sha256(pcm).hexdigest() != s['sha256']:
                    raise ValueError('样本文件内容已变化')
                decoded.append((s, np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768))
            report = {'sample_count': len(decoded), 'threshold_note': '各模型独立阈值尚未校准；以下为当前门槛下的独立录音测试，非模型准确率保证。',
                'models': {}, 'cloud': {'status': 'not_run', 'reason': '需要同一登记录音的 HTTPS 地址；尚未配置上传服务。'}}
            for name, factory in self.models.items():
                started = perf_counter()
                try:
                    model = factory()
                    load_ms = (perf_counter() - started) * 1000
                    if model is None:
                        raise ValueError('模型尚未下载或依赖未安装')
                    vectors, latencies = {}, []
                    with model.lock:
                        for s, audio in decoded:
                            began = perf_counter()
                            vector = model.embedding(audio)
                            latencies.append((perf_counter() - began) * 1000)
                            if vector is not None and math.isfinite(cosine(vector, vector)) and cosine(vector, vector) > .99:
                                vectors[s['id']] = np.asarray(vector) / np.linalg.norm(vector)
                    enrolled = {}
                    for s, _ in decoded:
                        if s['role'] == 'enroll' and s['id'] in vectors:
                            enrolled.setdefault(s['label'], []).append(vectors[s['id']])
                    # Exercise the real enrollment gate in an isolated bank,
                    # not merely report that embeddings can be extracted.
                    enrollment_labels = {s['label'] for s, _ in decoded if s['role']=='enroll'}
                    class TrialProfiles:
                        def get(self, key): return key if key in enrollment_labels else None
                        def add_carrier(self, *args): pass
                    isolated = LocalSpeakers(getattr(model, 'extractor', None), model.model_id,
                        self.root / ('bank-' + re.sub('[^a-zA-Z0-9]', '_', name) + '.json'), TrialProfiles(),
                        threshold=model.threshold, margin=model.margin, enrollment_threshold=getattr(model, 'enrollment_threshold', .8))
                    isolated.known = {}
                    isolated.embedding = model.embedding
                    accepted, failures = {}, {}
                    with model.lock:
                        for label in sorted({s['label'] for s, _ in decoded if s['role']=='enroll'}):
                            clips = [self._pcm(s) for s, _ in decoded if s['role']=='enroll' and s['label']==label]
                            try:
                                isolated.enroll_samples(clips, label)
                                accepted[label] = next(e['embedding'] for e in isolated.known.values() if e['object_id']==label)
                            except ValueError as exc:
                                failures[label] = str(exc)
                    centroids = accepted
                    results, misses, wrong, false_accepts, unknowns, known_tests = [], 0, 0, 0, 0, 0
                    labels = {s['label'] for s, _ in decoded if s['role'] == 'enroll'}
                    for s, _ in decoded:
                        if s['role'] != 'test': continue
                        scores = sorted(((cosine(vectors[s['id']], v), label) for label, v in centroids.items()), reverse=True) if s['id'] in vectors else []
                        predicted = scores[0][1] if scores and scores[0][0] >= model.threshold and (len(scores)<2 or scores[0][0]-scores[1][0] >= model.margin) else None
                        known = s['label'] in labels
                        if known:
                            known_tests += 1
                            misses += predicted is None
                            wrong += predicted is not None and predicted != s['label']
                        else:
                            unknowns += 1
                            false_accepts += predicted is not None
                        results.append({'sample_id': s['id'], 'expected': s['label'], 'predicted': predicted,
                            'score': round(scores[0][0], 3) if scores else None, 'second_score': round(scores[1][0], 3) if len(scores)>1 else None,
                            'correct': predicted == s['label'] if known else predicted is None})
                    total_enroll = sum(s['role'] == 'enroll' for s, _ in decoded)
                    report['models'][name] = {'status': 'ok', 'model_id': model.model_id,
                        'threshold': model.threshold, 'margin': model.margin, 'enrollment_samples': total_enroll,
                        'enrollment_extracted': sum(s['role']=='enroll' and s['id'] in vectors for s, _ in decoded),
                        'enrollment_people': len(accepted)+len(failures), 'enrollment_accepted': len(accepted), 'enrollment_failures': failures,
                        'known_tests': known_tests, 'misses': misses, 'wrong_identity': wrong,
                        'unknown_tests': unknowns, 'false_accepts': false_accepts,
                        'load_ms': round(load_ms, 1), 'embedding_p50_ms': round(median(latencies), 1),
                        'embedding_max_ms': round(max(latencies), 1), 'results': results}
                except Exception as exc:
                    report['models'][name] = {'status': 'unavailable', 'reason': str(exc)}
            self._save('report.json', report)
            return report

    def _pcm(self, sample):
        with wave.open(str(self.root / (sample['id'] + '.wav')), 'rb') as audio:
            return audio.readframes(audio.getnframes())
