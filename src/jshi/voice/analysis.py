"""Audio-only segmentation, overlap detection and speaker evidence. No transcription."""
from __future__ import annotations
from contextlib import nullcontext
import json
from types import SimpleNamespace
from threading import RLock
from time import monotonic
from .timing import phase, timed_lock, finish_phase

from .contracts import SpeechSegment


class SpeakerAnalyzer:
    def __init__(self, engine, speakers, *, build_engine=None, model=''):
        self.engine, self.speakers = engine, speakers
        self.engines = {model: engine} if model else {}
        self.build_engine = build_engine
        self.lock = RLock()
        self.parallel_identity = None

    def use(self, model, speakers):
        """Segment clustering and identity matching must share one embedding space."""
        if self.build_engine is not None and model not in self.engines:
            self.engines[model] = self.build_engine(model)
        self.engine = self.engines.get(model, self.engine)
        self.speakers = speakers

    def analyze(self, samples, offset_ms):
        with timed_lock(self.lock, 'analyzer_lock_ms'):
            return self._analyze(samples, offset_ms)

    def identify_segments(self, segments):
        """04 adds named evidence to 06's acoustic segments, without resegmentation."""
        if self.parallel_identity is not None:
            return tuple(self.parallel_identity.identify(s) for s in segments)
        if self.speakers is None:
            return segments
        output = []
        from dataclasses import replace
        for segment in segments:
            if segment.overlap:
                output.append(segment)
                continue
            lock = getattr(self.speakers, 'lock', None)
            with timed_lock(lock, 'speaker_lock_ms') if lock is not None else nullcontext():
                with phase('speaker_match_ms'):
                    if hasattr(self.speakers, 'identify_timed'):
                        track, vpid, confidence = self.speakers.identify_timed(
                            segment.samples, source_track=segment.track_id,
                            start_ms=segment.start_ms, end_ms=segment.end_ms)
                    else:
                        track, vpid, confidence = self.speakers.identify(segment.samples)
                    match = dict(getattr(self.speakers, 'last_match', {}))
            output.append(replace(segment, track_id=track, voiceprint_id=vpid,
                confidence=confidence, speaker_cluster_id=segment.track_id,
                identity_tentative=not bool(vpid), identity_note=json.dumps(match, ensure_ascii=False)))
        return tuple(output)

    def _analyze(self, samples, offset_ms):
        with phase('segmentation_model_ms'):
            original = self.engine.process(samples).sort_by_start_time()
        split_started = monotonic()
        # Split at other voices' boundaries before deciding which audio is mixed.
        # A short interruption must not contaminate a whole long speech span.
        spans = []
        for span in original:
            boundaries = {span.start, span.end}
            others = [o for o in original if o.speaker != span.speaker
                      and min(span.end, o.end) > max(span.start, o.start)]
            for other in others:
                boundaries.add(max(span.start, other.start))
                boundaries.add(min(span.end, other.end))
            points = sorted(boundaries)
            for start, end in zip(points, points[1:]):
                if end <= start:
                    continue
                mixed = any(min(end, o.end) > max(start, o.start) for o in others)
                if (spans and spans[-1].speaker == span.speaker and spans[-1].mixed == mixed
                        and 0 <= start - spans[-1].end <= .3):
                    spans[-1].end = end
                else:
                    spans.append(SimpleNamespace(start=start, end=end, speaker=span.speaker, mixed=mixed))
        spans.sort(key=lambda s: (s.start, s.end))
        finish_phase('overlap_split_ms', split_started)
        output = []
        for i, span in enumerate(spans):
            start = max(0, int(span.start * 16000))
            end = min(len(samples), int(span.end * 16000))
            if end <= start:
                continue
            overlap = span.mixed
            audio = samples[start:end]
            if overlap:
                # Mixed source audio cannot safely enroll or identify a person.
                track, vpid, confidence = f"overlap-{offset_ms}-{i}", "", None
                match = {'reason': '声音重叠，不据此确认姓名'}
                note = json.dumps(match, ensure_ascii=False)
            elif self.speakers is None:
                track, vpid, confidence = f'window-{offset_ms}-{span.speaker}', '', None
                match, note = {}, ''
            else:
                lock = getattr(self.speakers, 'lock', None)
                with timed_lock(lock, 'speaker_lock_ms') if lock is not None else nullcontext():
                    with phase('speaker_match_ms'):
                        if hasattr(self.speakers, 'identify_timed'):
                            track, vpid, confidence = self.speakers.identify_timed(audio, source_track=str(span.speaker),
                                start_ms=offset_ms + start // 16, end_ms=offset_ms + end // 16)
                        else:
                            track, vpid, confidence = self.speakers.identify(audio)
                        match = dict(getattr(self.speakers, 'last_match', {}))
                        note = json.dumps(match, ensure_ascii=False)
            output.append(SpeechSegment(audio, track, offset_ms + start // 16, offset_ms + end // 16,
                                     overlap, vpid, confidence,
                                     speaker_cluster_id=track if not overlap and not track.startswith('unidentified-') else '',
                                     identity_tentative=not overlap and not vpid and not track.startswith('unidentified-')
                                         and match.get('tentative', True), identity_note=note))
        return tuple(output)



class ParallelIdentity:
    """Bounded identity jobs, reusable across proven continuous clean speech."""
    def __init__(self, speakers, personal, report=None, *, budget_s=1.2, segments=None):
        from concurrent.futures import ThreadPoolExecutor
        self.speakers, self.personal, self.report = speakers, personal, report
        self.voice_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='voice-identity')
        self.mlp_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mlp-identity')
        self.lock = RLock()
        self.jobs, self.predictions, self.warmed = {}, {}, {}
        self.closed = False
        self.budget_s = budget_s
        self.segment_source = segments
        self.scopes = []

    def _voice_version(self):
        path = getattr(self.speakers, 'path', None)
        return (getattr(self.speakers, 'model_id', ''), id(getattr(self.speakers, 'extractor', None)),
                path.stat().st_mtime_ns if path and path.exists() else None,
                getattr(self.speakers, 'threshold', None),
                getattr(self.speakers, 'confirmation_threshold', None),
                getattr(self.speakers, 'margin', None))

    def _continuous_job(self, track, start, end, lane, version):
        if not track or self.segment_source is None:
            return None
        for scope in reversed(self.scopes):
            original = scope['segment']
            job = scope.get(lane)
            if (job is None or scope.get(lane+'_version') != version
                    or original.track_id != track or original.end_ms-original.start_ms < 1500):
                continue
            if job.done():
                if job.cancelled() or job.exception() is not None:
                    continue
                if lane == 'voice' and job.result().track_id.startswith('unidentified-'):
                    continue  # no usable evidence yet; a longer sample can help
            if self.segment_source.continuous_voice(track, min(start, original.start_ms),
                                                     max(end, original.end_ms)):
                return job
        return None

    def _key(self, pcm):
        from hashlib import sha256
        return (getattr(self.speakers, 'model_id', ''), id(getattr(self.speakers, 'extractor', None)),
                sha256(pcm).digest())

    def _mlp_version(self):
        if self.personal is None:
            return ()
        store = self.personal.store
        # Includes settings/labels, target selection and active trained versions.
        return (store.revision()[1], repr(store.settings()), tuple(
            (r['object_id'], repr(self.personal.manifest(r['object_id'])['active']))
            for r in store.targets()))

    def _run(self, lane, start, end, work):
        from .timing import report_safely
        began = monotonic()
        status = 'completed'
        report_safely(self.report, 'voice_identity_work', {'stage':'started', 'lane':lane,
            'start_ms':start, 'end_ms':end})
        try:
            return work()
        except BaseException as exc:
            status = type(exc).__name__
            raise
        finally:
            report_safely(self.report, 'voice_identity_work', {'stage':'finished', 'lane':lane,
                'start_ms':start, 'end_ms':end, 'status':status,
                'elapsed_ms':round((monotonic()-began)*1000,3)})

    @staticmethod
    def _trim(cache):
        for key in list(cache):
            if len(cache) <= 64:
                break
            if cache[key].done():
                del cache[key]

    def predict(self, pcm, start=0, end=0, *, track=''):
        if self.personal is None:
            return None
        version = self._mlp_version()
        key = (*self._key(pcm), version)
        with self.lock:
            if self.closed:
                return None
            reused = self._continuous_job(track, start, end, 'mlp', version)
            if reused is not None:
                from .timing import report_safely
                report_safely(self.report, 'voice_identity_work', {'stage':'reused', 'lane':'mlp',
                    'start_ms':start, 'end_ms':end, 'basis':'continuous_clean_voice'})
                return reused
            if key not in self.predictions:
                if sum(not f.done() for f in self.predictions.values()) >= 8:
                    return None
                job = self.mlp_pool.submit(self._run, 'mlp', start, end,
                    lambda: self.personal.predict(pcm, self.speakers))
                job.identity_audio_bounds = (start, end)
                job.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
                self.predictions[key] = job
                self._trim(self.predictions)
            return self.predictions[key]

    def start(self, segment):
        if segment.overlap or self.speakers is None:
            return None
        import numpy as np
        from .timing import report_safely
        pcm = np.clip(np.asarray(segment.samples)*32768, -32768, 32767).astype('<i2').tobytes()
        version = self._voice_version()
        key = (*self._key(pcm), segment.start_ms, segment.end_ms, segment.track_id, version)
        with self.lock:
            if self.closed:
                return None
            reused = self._continuous_job(segment.track_id, segment.start_ms, segment.end_ms, 'voice', version)
            if reused is not None:
                job = reused
                report_safely(self.report, 'voice_identity_work', {'stage':'reused', 'lane':'voiceprint',
                    'start_ms':segment.start_ms, 'end_ms':segment.end_ms,
                    'basis':'continuous_clean_voice'})
            else:
                if key not in self.jobs:
                    if sum(not f.done() for f in self.jobs.values()) >= 8:
                        return None
                    analyzer = SpeakerAnalyzer(None, self.speakers)
                    job = self.voice_pool.submit(self._run, 'voiceprint', segment.start_ms, segment.end_ms,
                        lambda: analyzer.identify_segments((segment,))[0])
                    job.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
                    self.jobs[key] = job
                    self._trim(self.jobs)
                job = self.jobs[key]
            # Reserve both lanes under one lock so concurrent 04/06 requests
            # cannot start a second job before the first scope is registered.
            mlp, mlp_version = None, None
            try:
                mlp_version = self._mlp_version()
                mlp = self.predict(pcm, segment.start_ms, segment.end_ms, track=segment.track_id)
            except Exception as exc:
                report_safely(self.report, 'voice_identity_work', {'stage':'failed', 'lane':'mlp',
                    'start_ms':segment.start_ms, 'end_ms':segment.end_ms, 'error_type':type(exc).__name__})
            if not any(s['voice'] is job and s.get('mlp') is mlp for s in self.scopes):
                origin = next((s['segment'] for s in self.scopes if s['voice'] is job), segment)
                self.scopes.append({'segment':origin, 'voice':job, 'voice_version':version,
                                    'mlp':mlp, 'mlp_version':mlp_version})
                self.scopes = self.scopes[-64:]
            return job

    def offer(self, segments, end):
        from dataclasses import replace
        for s in segments:
            if s.overlap or s.end_ms-s.start_ms < 1500:
                continue
            previous = self.warmed.get(s.track_id, -1)
            ended = end-s.end_ms >= 300
            if s.end_ms <= previous or (not ended and s.end_ms-max(s.start_ms, previous) < 6000):
                continue
            if previous >= s.start_ms and s.end_ms-previous < 300:
                continue  # small boundary jitter is not a new useful sample
            # Bound identity input as well; eight seconds is a context limit,
            # not a timer forcing every speaker to wait for eight seconds.
            a = max(s.start_ms, s.end_ms-6000) if ended else max(s.start_ms, previous)
            b = s.end_ms if ended else min(s.end_ms, a+6000)
            scope = replace(s, start_ms=a, end_ms=b,
                samples=s.samples[(a-s.start_ms)*16:(b-s.start_ms)*16])
            if self.start(scope) is not None:
                self.warmed[s.track_id] = b
        while len(self.warmed) > 128:
            self.warmed.pop(next(iter(self.warmed)))

    def identify(self, segment):
        job = self.start(segment)
        if job is None:
            return segment
        try:
            result = job.result(timeout=self.budget_s)
            if (result.start_ms, result.end_ms) == (segment.start_ms, segment.end_ms):
                return result
            from dataclasses import replace
            note = json.loads(result.identity_note or '{}')
            note['identity_basis'] = '同一段连续清晰声音，沿用先前人物证据；不是对新增尾部重新比对'
            return replace(segment, track_id=result.track_id, voiceprint_id=result.voiceprint_id,
                confidence=result.confidence, speaker_cluster_id=segment.track_id,
                identity_tentative=result.identity_tentative,
                identity_note=json.dumps(note, ensure_ascii=False))
        except Exception:
            # A failed voiceprint must not discard words or cancel the MLP lane.
            return segment

    def close(self):
        with self.lock:
            self.closed = True
        self.voice_pool.shutdown(wait=True, cancel_futures=True)
        self.mlp_pool.shutdown(wait=True, cancel_futures=True)
