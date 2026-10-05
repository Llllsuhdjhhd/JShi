"""CPU streaming recognition and speaker embeddings; no cloud audio upload."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from pathlib import Path
from uuid import uuid4
from threading import RLock
from time import monotonic, time

from .volc import Transcript
from .references import VoiceCandidates, references, score as reference_score, MAX_REFERENCES


ASR_NAME = "sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01"
SPEAKER_NAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
SPEAKER_MODELS = {'cam++': SPEAKER_NAME, 'eres2netv2': '3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx'}
REFINER_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
SEGMENTATION_NAME = "sherpa-onnx-pyannote-segmentation-3-0"
# CAM++ trains on 3-second crops and concatenates short clips to at least 6 seconds.
# One enrollment embedding is stable around 10 seconds; longer household audio starts to dilute it.
VOICEPRINT_SECONDS = 10


def cosine(a, b) -> float:
    if len(a) != len(b) or len(a) == 0:
        return -1.0
    denom = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / denom if denom else -1.0


def voiceprint_score(vector, entry) -> float:
    """Reference count never adds points; all callers use the same mean score."""
    return reference_score(vector, entry)['score']


def fit_voice_pcm(new_pcm: bytes, old_pcm: bytes, *, seconds: float = VOICEPRINT_SECONDS) -> bytes:
    """Newest audio is placed first. Short audio keeps its real length; the oldest tail is dropped past the limit."""
    limit = int(16000 * seconds) * 2
    combined = (new_pcm or b'') + (old_pcm or b'')
    if len(combined) % 2:
        combined = combined[:-1]
    return combined[:limit]


def _unit(vector):
    import numpy as np
    values = np.asarray(vector, dtype='float64')
    norm = np.linalg.norm(values)
    if norm <= 0 or not np.isfinite(norm):
        return None
    return (values / norm).tolist()


class EnrollmentError(ValueError):
    def __init__(self, message, sample_indices=()):
        super().__init__(message)
        self.sample_indices = tuple(sample_indices)


class LocalSpeakers:
    def __init__(self, extractor, model_id: str, path: Path, profiles,
                 *, threshold: float = 0.65, margin: float = 0.08, enrollment_threshold: float = 0.8,
                 anonymous_ttl_s: float = 7 * 86400, clock=time) -> None:
        self.extractor, self.model_id, self.path, self.profiles = extractor, model_id, path, profiles
        self.threshold, self.margin = threshold, margin
        self.confirmation_threshold = max(threshold, .65)
        # Unnamed voices are remembered briefly, like a stranger met in passing.
        self.anonymous_ttl_s, self.clock = anonymous_ttl_s, clock
        self.enrollment_threshold = enrollment_threshold
        if not -1 <= threshold <= 1 or margin < 0:
            raise ValueError("invalid voice similarity thresholds")
        if not -1 <= enrollment_threshold <= 1:
            raise ValueError('invalid enrollment similarity threshold')
        self.lock = RLock()
        self.known: dict[str, dict] = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("model_id") == model_id:
                self.known = data.get("entries", {})
                self._remove_withdrawn()
                self._collapse_entries()
        self.candidates = VoiceCandidates(path.with_suffix('.candidates.sqlite3'), clock=clock)
        self.tracks: dict[str, list[float]] = {}
        self.track_samples: dict[str, list[list[float]]] = {}
        self.pending_tracks: list[dict] = []
        self.last_embedding: dict[str, list[float]] = {}
        self.binding: dict[str, str] = {}
        self.last_match = {}
        self.recent_tracks = {}
        self.named_tracks = {}
        self.observation = 0
        settings = self.path.with_suffix('.settings.json')
        if settings.is_file():
            saved = json.loads(settings.read_text(encoding='utf-8'))
            if saved.get('model_id') == model_id:
                value = saved.get('match_threshold')
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .3 <= value <= .95:
                    raise ValueError('声纹匹配门槛设置无效')
                self.threshold = float(value)
                confirmation = saved.get('single_confirmation_threshold', max(value, .65))
                if isinstance(confirmation, bool) or not isinstance(confirmation, (int, float)) or not math.isfinite(confirmation) or not .3 <= confirmation <= .95:
                    raise ValueError('声纹姓名确认门槛设置无效')
                self.confirmation_threshold = float(confirmation)

    def set_threshold(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .3 <= value <= .95:
            raise ValueError('声纹匹配门槛须在 0.30–0.95 之间')
        with self.lock:
            target = self.path.with_suffix('.settings.json')
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix('.tmp')
            temp.write_text(json.dumps({'model_id': self.model_id, 'match_threshold': value,
                                        'single_confirmation_threshold': value}), encoding='utf-8')
            temp.replace(target)
            self.threshold = float(value)
            self.confirmation_threshold = float(value)
            return self.threshold

    def embedding(self, samples):
        if self.extractor is None or len(samples) < 16000 * 1.5:
            return None
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=samples)
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            return None
        values = list(map(float, self.extractor.compute(stream)))
        if not values or not all(math.isfinite(x) for x in values):
            return None
        return values

    def rank_known(self, samples, limit: int = 3) -> list[dict]:
        """得分最高的已知声纹。未达门槛也返回，供上下文复判；提不出特征时返回空列表。"""
        with self.lock:
            self._expire()
            vector = self.embedding(samples)
            if vector is None:
                return []
            ranked = []
            for _, item in self._person_entries():
                profile = self.profiles.get(item.get("object_id"))
                if profile is None or profile.status == "rejected" or not item.get("embedding"):
                    continue
                if item.get('expires_at') is not None or (profile.source == 'voice_anonymous' and profile.label.startswith('未命名访客')):
                    continue
                from .identity import canonical_voice_profile
                profile = canonical_voice_profile(self.profiles, profile)
                stats = reference_score(vector, item)
                ranked.append((stats['score'], profile.object_id, profile.label, stats))
            ranked.sort(key=lambda row: row[:3], reverse=True)
            seen = set()
            result = []
            for score, object_id, label, stats in ranked:
                if object_id in seen:
                    continue
                seen.add(object_id)
                result.append({**stats, "object_id": object_id, "label": label, "score": round(score, 3)})
                if len(result) >= limit:
                    break
            return result

    def identify(self, samples) -> tuple[str, str, float | None]:
        with self.lock:
            return self._identify(samples)

    def identify_timed(self, samples, *, source_track='', start_ms=None, end_ms=None):
        with self.lock:
            return self._identify(samples, source_track=source_track, start_ms=start_ms, end_ms=end_ms)

    def _save_bank(self):
        self._remove_withdrawn()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"model_id": self.model_id, "entries": self.known}), encoding="utf-8")
        temp.replace(self.path)

    def _remove_withdrawn(self):
        """Explicitly deleted samples stay deleted even after a stale bank save."""
        path = self.path.with_suffix('.withdrawn.json')
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('model_id') == self.model_id:
            for key in data.get('keys', ()):
                self.known.pop(key, None)

    def _collapse_entries(self):
        """Keep legacy bank bytes intact; group references at scoring time."""
        return

    def _person_entries(self):
        """One person competes once, even when an old bank has multiple keys."""
        grouped = {}
        for key, entry in self.known.items():
            grouped.setdefault(entry.get('object_id'), []).append((key, entry))
        for object_id, items in grouped.items():
            items.sort(key=lambda pair: pair[1].get('expires_at') is not None)
            scoring_items = items if items[0][1].get('expires_at') is not None else [pair for pair in items if pair[1].get('expires_at') is None]
            rows = [row for _, item in scoring_items for row in references(item)]
            yield items[0][0], {**items[0][1], 'references': references({'references': rows})}

    def _pcm_of(self, object_id) -> bytes:
        for entry in self.known.values():
            if entry.get('object_id') == object_id and entry.get('pcm'):
                return base64.b64decode(entry['pcm'])
        return b''

    def saved_reference_counts(self) -> list[dict]:
        """Effective saved references for this model, grouped by person."""
        with self.lock:
            result = []
            for _, entry in self._person_entries():
                profile = self.profiles.get(entry['object_id'])
                if not profile or profile.status == 'rejected' or entry.get('expires_at') is not None:
                    continue
                result.append({'object_id': profile.object_id, 'label': profile.label,
                               'reference_count': len(references(entry))})
            return result

    def rank_short_hint(self, pcm: bytes) -> dict:
        """Repeat a short clip for feature extraction, never identity binding."""
        import numpy as np
        if len(pcm) % 2 or not 6400 <= len(pcm) < 48000:
            return {}
        repetitions = math.ceil(96000 / len(pcm))
        samples = np.frombuffer((pcm * repetitions)[:96000], dtype='<i2').astype('float32') / 32768
        ranked = self.rank_known(samples)
        return {'original_seconds': len(pcm) / 32000, 'repeat_count': repetitions,
                'feature_seconds': len(samples) / 16000, 'candidates': ranked,
                'basis': '同一短句首尾重复拼接，仅供参考，不增加独立证据',
                'calibrated_probability': False}

    def _store_voice(self, object_id, embedding, pcm, *, basis, expires_at=None, new_references=None) -> bool:
        vector = _unit(embedding)
        if vector is None or self.profiles.get(object_id) is None:
            return False
        keys = [k for k, e in self.known.items() if e.get('object_id') == object_id]
        key = keys[0] if keys else uuid4().hex
        previous = self.known[keys[0]] if keys else {}
        rows = [row for old_key in keys for row in references(self.known[old_key])]
        entry = {'object_id': object_id, 'embedding': vector, 'basis': basis}
        if rows or new_references:
            # Explicit new selections update a full bank. Keep the newest
            # observations first; deduplicate before applying the ten-row cap.
            merged = [*reversed(new_references or ()), *rows]
            if new_references:
                merged.sort(key=lambda row: row.get('at') or 0, reverse=True)
            entry['references'] = references({'references': merged})
        if pcm:
            entry['pcm'] = base64.b64encode(pcm).decode('ascii')
            entry['seconds'] = round(len(pcm) / 32000, 2)
        elif previous.get('pcm'):
            entry['pcm'] = previous['pcm']
            entry['seconds'] = previous.get('seconds')
        if expires_at is not None:
            entry['expires_at'] = expires_at
        self.known[key] = entry
        try:
            self._save_bank()
        except Exception:
            if keys:
                self.known[key] = previous
            else:
                self.known.pop(key, None)
            raise
        if not keys:
            from jshi.recognition import CarrierEntry
            self.profiles.add_carrier(object_id, CarrierEntry('voiceprint', f'local:{self.model_id}:{key}', basis))
        return True

    def _expire(self):
        self._remove_withdrawn()
        now = self.clock()
        expired = [k for k, e in self.known.items() if e.get('expires_at') is not None and e['expires_at'] <= now]
        for key in expired:
            del self.known[key]
        if expired:
            self._save_bank()

    def remember_anonymous(self, track: str, object_id: str) -> bool:
        """Keep an unnamed visitor's voice for a limited time."""
        with self.lock:
            if self.binding.get(track) == object_id:
                return True
            samples = self.track_samples.get(track)
            if samples:
                import numpy as np
                rows = [np.asarray(v, dtype='float64') for v in samples]
                rows = [v / np.linalg.norm(v) for v in rows if np.linalg.norm(v) > 0]
                if rows:
                    center = np.mean(rows, axis=0)
                    norm = np.linalg.norm(center)
                    if norm > 0:
                        self.last_embedding[track] = (center / norm).tolist()
            return self._bind(track, object_id, basis='anonymous_voice', expires_at=self.clock() + self.anonymous_ttl_s)

    def confirm(self, object_id: str) -> None:
        """A known name turns a short-term voice into a lasting one."""
        with self.lock:
            changed = False
            for entry in self.known.values():
                if entry['object_id'] == object_id and 'expires_at' in entry:
                    del entry['expires_at']
                    changed = True
            if changed:
                self._save_bank()

    def forget(self, object_id: str) -> None:
        with self.lock:
            keys = [k for k, e in self.known.items() if e['object_id'] == object_id]
            for key in keys:
                del self.known[key]
            old_tracks = {t for t, o in self.binding.items() if o == object_id}
            old_tracks.update(t for oid, t in self.named_tracks.items() if oid == object_id)
            self.named_tracks.pop(object_id, None)
            for track in old_tracks:
                for table in (self.binding, self.tracks, self.track_samples, self.last_embedding, self.recent_tracks):
                    table.pop(track, None)
            if keys:
                self._save_bank()

    def rank_vector(self, vector):
        """Score against registered people without touching session tracks or learning."""
        from .identity import canonical_voice_profile
        with self.lock:
            self._expire()
            best = {}
            for _, item in self._person_entries():
                profile = self.profiles.get(item['object_id'])
                if profile is None or profile.status == 'rejected':
                    continue
                if item.get('expires_at') is not None or (profile.source == 'voice_anonymous' and profile.label.startswith('未命名访客')):
                    continue
                object_id = canonical_voice_profile(self.profiles, profile).object_id
                best[object_id] = max(best.get(object_id, -2.0), float(voiceprint_score(vector, item)))
            return sorted(((score, object_id) for object_id, score in best.items()), reverse=True)

    def reset_session(self):
        with self.lock:
            for items in (self.tracks, self.track_samples, self.pending_tracks,
                          self.last_embedding, self.binding, self.recent_tracks, self.named_tracks):
                items.clear()
            self.observation = 0
            self.last_match = {}

    def _identify(self, samples, *, source_track='', start_ms=None, end_ms=None) -> tuple[str, str, float | None]:
        self.observation += 1
        now = end_ms / 1000 if end_ms is not None else monotonic()
        vector = self.embedding(samples)
        if vector is None:
            self.last_match = {'reason': '样本不足或无法提取声纹', 'seconds': round(len(samples)/16000, 2)}
            return f"unidentified-{uuid4().hex}", "", None
        self._expire()
        by_person = {}
        for key, item in self._person_entries():
            profile = self.profiles.get(item["object_id"])
            if profile is None or profile.status == "rejected":
                continue
            if profile.source == 'voice_anonymous' and profile.label.startswith('未命名访客'):
                continue
            if item.get('expires_at') is not None:
                continue
            from .identity import canonical_voice_profile
            profile = canonical_voice_profile(self.profiles, profile)
            hit = (voiceprint_score(vector, item), key)
            if hit > by_person.get(profile.object_id, (-2, "")):
                by_person[profile.object_id] = hit
        known = sorted(by_person.values(), reverse=True)
        # Temporary observations never compete with registered names.
        self.last_match = {'reason': '没有可用登记' if not known else '本句尚未确认姓名',
            'best_score': round(known[0][0], 3) if known else None,
            'runner_up': round(known[1][0], 3) if len(known) > 1 else None,
            'threshold': self.threshold, 'margin': self.margin,
            'single_confirmation_threshold': self.confirmation_threshold,
            'candidates': [{**reference_score(vector, dict(self._person_entries())[key]), 'object_id': oid, 'label': self.profiles.get(oid).label, 'score': round(score, 3), 'source': 'voiceprint'}
                for oid, (score, key) in sorted(by_person.items(), key=lambda pair: pair[1], reverse=True)[:3]]}
        group = self._collect_pending(vector, len(samples)/16000, now, start_ms, end_ms)
        if known and known[0][0] >= self.confirmation_threshold and (len(known) < 2 or known[0][0]-known[1][0] >= self.margin):
            return self._matched_voice(vector, known[0][1], known[0][0], now, source_track)
        if group is None:
            self.last_match.update(tentative=True, anonymous_reason='接近多个待定声音，无法分组；不另建人物')
            return f'unidentified-{uuid4().hex}', '', None
        track = group['track']
        self.last_embedding[track] = vector
        examples = group['samples']
        center = _unit([sum(col)/len(examples) for col in zip(*examples)])
        from statistics import median
        cohesion = sorted(cosine(center, v) for v in examples)
        pairwise = [cosine(a, b) for i, a in enumerate(examples) for b in examples[i+1:]]
        pair_median = median(pairwise) if pairwise else 1.
        stable = len(examples) >= 10 and group['seconds'] >= 15 and median(cohesion) >= .75 and cohesion[len(cohesion)//4] >= .65 and pair_median >= .65
        self.last_match.update(tentative=not stable,
            collection={'count': len(examples), 'required_count': 10, 'seconds': round(group['seconds'], 2),
                'median_cohesion': round(median(cohesion), 3), 'lower_quartile_cohesion': round(cohesion[len(cohesion)//4], 3),
                'median_pair_similarity': round(pair_median, 3),
                'stable': stable},
            anonymous_reason='已收集多段一致声音，仅确认声音组，未确认姓名' if stable else '收集待定声音；至少十段有效、不重叠发言后核对分布，不参与姓名竞争')
        if stable or (len(examples) >= 2 and median(cohesion) >= .80 and pair_median >= .75):
            # Recompute the distribution against the current bank; never train a
            # registered voice from uncertain observations.
            ranked = []
            for oid, (_, key) in by_person.items():
                scores = [voiceprint_score(v, dict(self._person_entries())[key]) for v in examples]
                ranked.append((sum(scores)/len(scores), oid, key, scores))
            ranked.sort(reverse=True)
            if ranked:
                score, oid, key, scores = ranked[0]
                match_floor = max(self.threshold, min(.58, self.confirmation_threshold) if stable else self.confirmation_threshold)
                support = sum(value >= match_floor for value in scores)/len(scores)
                gap = score-ranked[1][0] if len(ranked) > 1 else None
                if score >= match_floor and support >= .8 and (gap is None or gap >= self.margin):
                    self.last_match['collection'].update(pick=oid, mean_match=round(score, 3), support_fraction=round(support, 3))
                    return self._matched_voice(vector, key, score, now, source_track)
        if stable:
            # Compare stable unnamed groups in a separate pool, after the named
            # decision. They never affect the named ranking or its margin.
            anonymous = []
            for key, entry in self.known.items():
                profile = self.profiles.get(entry['object_id'])
                if profile and profile.status != 'rejected' and profile.source == 'voice_anonymous' and profile.label.startswith('未命名访客'):
                    scores = sorted(voiceprint_score(v, entry) for v in examples)
                    if median(scores) >= .70 and scores[len(scores)//4] >= .58:
                        anonymous.append((median(scores), key))
            anonymous.sort(reverse=True)
            if anonymous and (len(anonymous) < 2 or anonymous[0][0]-anonymous[1][0] >= .08):
                score, key = anonymous[0]
                self.last_embedding[track] = vector
                self.last_match['anonymous_reason'] = '多段分布支持已保存的临时声音组，仍未确认姓名'
                return track, f'local:{self.model_id}:{key}', score
            self.tracks[track] = center
            self.track_samples[track] = list(examples)
            self.recent_tracks[track] = (now, source_track)
        return track, '', None

    def _matched_voice(self, vector, key, score, now, source_track):
        from .identity import canonical_voice_profile
        object_id = canonical_voice_profile(self.profiles, self.profiles.get(self.known[key]['object_id'])).object_id
        track = self.named_tracks.setdefault(object_id, 'known-' + object_id)
        self.last_match.update(reason='多段分布支持已有姓名' if self.last_match.get('collection', {}).get('mean_match') is not None else '已匹配登记声纹', tentative=False)
        self.last_embedding[track] = vector
        self.binding[track] = object_id
        self.tracks.setdefault(track, vector)
        self.track_samples.setdefault(track, [vector])
        self.recent_tracks[track] = (now, source_track)
        return track, f'local:{self.model_id}:{key}', score

    def _collect_pending(self, vector, seconds, now, start_ms, end_ms):
        # Keep a bounded observation pool for up to fifteen minutes. Diarization
        # track numbers are not identity evidence, and cannot merge groups.
        self.pending_tracks = [g for g in self.pending_tracks if now-g['time'] <= 900]
        matches = []
        for group in self.pending_tracks:
            center = _unit([sum(col)/len(group['samples']) for col in zip(*group['samples'])])
            matches.append((cosine(vector, center), group['track'], group))
        matches.sort(key=lambda row: row[:2], reverse=True)
        if matches and matches[0][0] >= .50:
            if len(matches) > 1 and matches[0][0]-matches[1][0] < .04:
                return None
            group = matches[0][2]
        else:
            group = {'track': 'pending-' + uuid4().hex[:10], 'samples': [], 'durations': [],
                'intervals': [], 'seconds': 0., 'time': now, 'count': 0}
            if len(self.pending_tracks) >= 32:
                self.pending_tracks.pop(0)
            self.pending_tracks.append(group)
        overlap = start_ms is not None and end_ms is not None and any(
            start_ms < end and end_ms > start for start, end in group['intervals'])
        if seconds >= 1.5 and not overlap:
            group['samples'].append(vector)
            group['durations'].append(seconds)
            if start_ms is not None and end_ms is not None:
                group['intervals'].append((start_ms, end_ms))
            group['samples'] = group['samples'][-20:]
            group['durations'] = group['durations'][-20:]
            group['intervals'] = group['intervals'][-64:]
            group.update(seconds=sum(group['durations']), count=len(group['samples']), time=max(now, group['time']))
        if not group['samples']:
            self.pending_tracks.remove(group)
            return None
        return group

    def discard_pending(self, track):
        """Explicit labeling retires only this observation group."""
        with self.lock:
            self.pending_tracks = [g for g in self.pending_tracks if g['track'] != track]

    def collect_candidate(self, pcm, input_id, actor_id, session_id, *, overlap=False, start_ms=None, end_ms=None):
        if not pcm or overlap or len(pcm) < 48000:
            return 'ineligible'
        import numpy as np
        # Bound feature extraction, including unusually long utterances.
        values = np.frombuffer(pcm[:320000], dtype='<i2').astype('float32')/32768
        with self.lock:
            vector = self.embedding(values)
            return self.candidates.collect(input_id, actor_id, session_id, vector, len(values)/16000,
                                           overlap=overlap, start_ms=start_ms, end_ms=end_ms)

    def review_candidate(self, input_id, object_id, *, agreement=False):
        """Existing people only; two sessions + independent semantics, never one match."""
        if not agreement:
            return 'unverified'
        with self.lock:
            entry = next(((key, row) for key, row in self._person_entries()
                          if row['object_id'] == object_id and row.get('expires_at') is None), None)
            profile = self.profiles.get(object_id)
            if not entry or not profile or profile.source == 'voice_anonymous':
                return 'not_registered'
            key, current = entry
            self.candidates.verify(input_id, object_id)
            rows = self.candidates.verified(object_id)
            if len({row['session_id'] for row in rows}) < 2:
                return 'awaiting_independent_session'
            eligible = []
            for row in rows:
                ranked = self.rank_vector(row['embedding'])
                if (row['quality'] >= .5 and ranked and ranked[0][1] == object_id
                    and ranked[0][0] >= max(self.threshold, self.enrollment_threshold)
                    and (len(ranked) == 1 or ranked[0][0]-ranked[1][0] >= self.margin)):
                    eligible.append(row)
            if len({row['session_id'] for row in eligible}) < 2:
                return 'insufficient_evidence'
            old_refs = references(current)
            new_refs = references({'references': [*old_refs, *[
                {'embedding': row['embedding'], 'quality': row['quality'], 'input_id': row['input_id'],
                 'session_id': row['session_id'], 'at': row['at'], 'basis': 'cross_session_voice_and_semantics'}
                for row in eligible]]})
            if len(new_refs) == len(old_refs):
                return 'full_or_duplicate'
            old = self.known[key]
            self.known[key] = {**old, 'references': new_refs}
            try:
                self._save_bank()
            except Exception:
                self.known[key] = old
                raise
            kept_ids = {row.get('input_id') for row in new_refs}
            self.candidates.mark_promoted([row['input_id'] for row in eligible if row['input_id'] in kept_ids])
            return 'promoted'

    def bind(self, track: str, object_id: str) -> bool:
        with self.lock:
            bound = self._bind(track, object_id)
            if bound:
                self.discard_pending(track)
            return bound

    def enroll_samples(self, clips, object_id: str, *, user_labeled: bool = False, sample_ids=None, session_id='') -> dict:
        """Trust explicit human labels; reserve consistency gates for unconfirmed samples."""
        import numpy as np
        vectors = []
        owners = []
        seconds = 0.0
        with self.lock:
            if self.profiles.get(object_id) is None:
                raise ValueError('对象不存在')
            durations = [len(pcm) / 32000 for pcm in clips]
            short = [i for i, d in enumerate(durations) if d < 1.5]
            if not clips or short:
                raise EnrollmentError('第 '+ '、'.join(str(i+1) for i in short)+' 段不足 1.5 秒；请取消这些短句，选择较长单人发言', short)
            if (len(clips) == 1 and durations[0] < 3) or (len(clips) > 1 and sum(durations) < 4):
                raise ValueError('单段至少 3 秒；多段合计至少 4 秒，请多选清晰发言')
            for index, pcm in enumerate(clips):
                values = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
                seconds += len(values) / 16000
                # Check inside long clips too, rather than hiding several
                # voices inside a single averaged embedding.
                for start in range(0, len(values), 48000):
                    window = values[start:start+48000]
                    if len(window) < 24000:
                        continue
                    vector = self.embedding(window)
                    if vector is not None:
                        vectors.append(vector)
                        owners.append((index, start / 16000))
                    else:
                        raise EnrollmentError(f'第 {index+1} 段 {start/16000:.1f} 秒处无法提取声纹；这不等于检测到了杂音，请换一段更完整的发言', [index])
            if not vectors:
                raise ValueError('有效声音不足，请再选择几段清晰发言（合计至少 1.5 秒）')
            normalized = []
            for v in vectors:
                a = np.asarray(v, dtype='float64')
                norm = np.linalg.norm(a)
                if norm <= 0 or not np.isfinite(norm):
                    raise ValueError('声纹特征无效，请换一组录音')
                normalized.append(a / norm)
            pairs = [(float(np.dot(a, b)), i, j) for i, a in enumerate(normalized) for j, b in enumerate(normalized) if j > i]
            weakest = min(pairs, default=None)
            minimum = weakest[0] if weakest else None
            if not user_labeled and minimum is not None and minimum < self.enrollment_threshold:
                first, second = owners[weakest[1]], owners[weakest[2]]
                raise EnrollmentError(f'片段一致性不足：第 {first[0]+1} 段（{first[1]:.1f} 秒处）与第 {second[0]+1} 段（{second[1]:.1f} 秒处）相似度 {minimum:.3f}，登记要求 {self.enrollment_threshold:.3f}。短句、收声变化或混选人物都可能造成，不能据此判定有杂音；请检查这两段。', sorted({first[0], second[0]}))
            # Callers pass older audio first. The newest clip is placed at the front.
            pcm = fit_voice_pcm(b''.join(reversed(clips)), self._pcm_of(object_id))
            samples = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
            vector = self.embedding(samples)
            new_rows = []
            for index, duration in enumerate(durations):
                parts = [v for v, owner in zip(normalized, owners) if owner[0] == index]
                ref = _unit([sum(col)/len(parts) for col in zip(*parts)]) if parts else None
                if ref:
                    new_rows.append({'embedding': ref, 'quality': min(duration/6, 1),
                                     'seconds': duration, 'at': self.clock(), 'session_id': session_id,
                                     'input_id': sample_ids[index] if sample_ids else 'manual-' + uuid4().hex,
                                     'basis': 'manual_selection'})
            if vector is None or not self._store_voice(object_id, vector, pcm, basis='manual_selection', new_references=new_rows):
                raise ValueError('声纹登记失败')
            stats = {'clips': len(clips), 'embeddings': 1, 'seconds': round(len(pcm) / 32000, 1),
                    'min_similarity': round(minimum, 3) if minimum is not None else None,
                    'enrollment_threshold': self.enrollment_threshold, 'voiceprint_seconds': VOICEPRINT_SECONDS}
            stats['reference_count'] = len(references(next(e for e in self.known.values() if e['object_id'] == object_id)))
            if user_labeled:
                stats.update(user_labeled=True,
                    warning='这几段声音差异较大，已按你的标注收成一条声纹；该分数不能证明混入了别人。' if minimum is not None and minimum < self.enrollment_threshold else '')
            return stats

    def _bind(self, track: str, object_id: str, *, basis: str = 'local_self_introduction', templates=None, expires_at=None) -> bool:
        del templates
        if self.binding.get(track) == object_id:
            if expires_at is None:
                self.confirm(object_id)
            return True
        vector = self.last_embedding.get(track)
        if vector is None or self.profiles.get(object_id) is None:
            return False
        # A later introduction must not add a second voiceprint beside an enrollment.
        if any(e.get('object_id') == object_id for e in self.known.values()):
            if expires_at is None:
                self.confirm(object_id)
            self.binding[track] = object_id
            return True
        if not self._store_voice(object_id, vector, b'', basis=basis, expires_at=expires_at):
            return False
        self.binding[track] = object_id
        return True


class LocalASR:
    def __init__(self, recognizer, speakers=None, refiner=None, diarizer=None) -> None:
        self.recognizer, self.speakers = recognizer, speakers
        self.refiner = refiner
        self.diarizer = diarizer
        self.early_speaker = None
        self.stream = recognizer.create_stream()
        self.offset_samples = 0
        self.segment_samples = 0
        self.samples = []
        self.last_text = ""

    def reset_session(self) -> None:
        self.stream = self.recognizer.create_stream()
        self.offset_samples = self.segment_samples = 0
        self.samples.clear()
        self.last_text = ""
        self.early_speaker = None
        if self.speakers:
            self.speakers.tracks.clear()
            self.speakers.track_samples.clear()
            self.speakers.pending_tracks.clear()
            self.speakers.last_embedding.clear()
            self.speakers.binding.clear()
            self.speakers.reset_session()

    def feed(self, pcm: bytes, *, final: bool = False) -> tuple[Transcript, ...]:
        import numpy as np
        if len(pcm) % 2:
            raise ValueError("PCM must be signed 16-bit little endian")
        values = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768
        if len(values):
            self.stream.accept_waveform(16000, values)
            self.samples.append(values)
            self.segment_samples += len(values)
        if final:
            self.stream.accept_waveform(16000, np.zeros(8000, dtype="float32"))
            self.stream.input_finished()
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        text = self.recognizer.get_result(self.stream).strip()
        endpoint = final or self.recognizer.is_endpoint(self.stream) or self.segment_samples >= 16000 * (6 if self.diarizer else 25)
        start, end = self.offset_samples // 16, (self.offset_samples+self.segment_samples) // 16
        output = []
        if endpoint and (text or (self.refiner is not None and self.samples)):
            audio = np.concatenate(self.samples) if self.samples else np.array([], dtype="float32")
            if self.diarizer is not None and len(audio):
                output.extend(self.diarizer.transcribe(audio, start))
                text = ""
            elif self.refiner is not None and len(audio):
                refined = self.refiner.create_stream()
                refined.accept_waveform(16000, audio)
                self.refiner.decode_stream(refined)
                text = refined.result.text.strip()
            text = re.sub(r"<\|.*?\|>|<unk>", "", text).strip()
            if text:
                track, vpid, confidence = self.speakers.identify_timed(audio, start_ms=start, end_ms=end) if self.speakers else (f"unidentified-{start}", "", None)
                output.append(Transcript(text, track, start, end, True, voiceprint_id=vpid, confidence=confidence, speaker_cluster_id=track if self.speakers and not track.startswith('unidentified-') else '', identity_tentative=bool(self.speakers and not vpid and not track.startswith('unidentified-') and getattr(self.speakers, 'last_match', {}).get('tentative', True))))
        elif text and text != self.last_text:
            if self.speakers and self.diarizer is None and self.segment_samples >= 24000 and self.early_speaker is None:
                self.early_speaker = self.speakers.identify(np.concatenate(self.samples))
            track, vpid, score = self.early_speaker or (f"pending-{start}", "", None)
            output.append(Transcript(text, track, start, end, False, voiceprint_id=vpid, confidence=score))
        self.last_text = text
        if endpoint and not final:
            self.recognizer.reset(self.stream)
            self.offset_samples += self.segment_samples
            self.segment_samples = 0
            self.samples.clear()
            self.last_text = ""
            self.early_speaker = None
        return tuple(output)


def build_speakers(data_dir: Path, profiles, *, model=None):
    model = model or os.getenv('JSHI_VOICE_SPEAKER_MODEL', 'cam++')
    if model not in SPEAKER_MODELS:
        raise ValueError('声纹模型须为 cam++ 或 eres2netv2')
    root = Path(os.getenv("JSHI_VOICE_LOCAL_DIR", str(data_dir / "voice_models")))
    speaker_model = root / SPEAKER_MODELS[model]
    if not speaker_model.is_file():
        return None
    try:
        import sherpa_onnx
    except ImportError:
        return None
    threads = max(1, min(4, int(os.getenv("JSHI_VOICE_LOCAL_THREADS", "2"))))
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(speaker_model), num_threads=threads, provider="cpu")
    if not cfg.validate():
        raise ValueError("invalid local speaker model")
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    model_id = hashlib.sha256(speaker_model.read_bytes()).hexdigest()[:16]
    bank = 'voiceprints.json' if model == 'cam++' else 'voiceprints-eres2netv2.json'
    return LocalSpeakers(extractor, model_id, data_dir / bank, profiles,
        threshold=float(os.getenv("JSHI_VOICE_MATCH_THRESHOLD", "0.65")),
        enrollment_threshold=float(os.getenv('JSHI_VOICE_ENROLL_THRESHOLD', '.8')),
        anonymous_ttl_s=float(os.getenv('JSHI_VOICE_ANONYMOUS_DAYS', '7')) * 86400)


def build_local(data_dir: Path, profiles) -> LocalASR:
    try:
        import sherpa_onnx
    except ImportError:
        raise ValueError('本地语音需要安装：python -m pip install -e ".[voice-local]"')
    root = Path(os.getenv("JSHI_VOICE_LOCAL_DIR", str(data_dir / "voice_models")))
    model_dir = root / ASR_NAME
    models = sorted(model_dir.glob("*.onnx"))
    tokens = model_dir / "tokens.txt"
    if len(models) != 1 or not tokens.is_file():
        raise ValueError("本地流式模型尚未准备。请先运行 jshi voice-setup，或设置 JSHI_VOICE_LOCAL_DIR。")
    threads = max(1, min(4, int(os.getenv("JSHI_VOICE_LOCAL_THREADS", "2"))))
    recognizer = sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(
        model=str(models[0]), tokens=str(tokens), num_threads=threads, provider="cpu",
        enable_endpoint_detection=True, rule1_min_trailing_silence=1.5,
        rule2_min_trailing_silence=1.2, rule3_min_utterance_length=25,
    )
    speakers = build_speakers(data_dir, profiles)
    refiner = None
    refine_mode = os.getenv("JSHI_VOICE_REFINE", "auto")
    if refine_mode not in {"auto", "on", "off"}:
        raise ValueError("JSHI_VOICE_REFINE 必须为 auto/on/off")
    refine_root = root / REFINER_NAME
    refine_models = sorted(refine_root.glob("*.onnx"))
    if refine_mode == "on" and not refine_models:
        raise ValueError("请先运行 jshi voice-setup --refine 下载整句识别模型")
    if refine_mode != "off" and refine_models:
        refiner = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(refine_models[0]), tokens=str(refine_root / "tokens.txt"),
            num_threads=threads, provider="cpu", language="zh", use_itn=True,
        )
    print("本地识别：流式预览 + SenseVoice 整句复核" if refiner else "本地识别：小模型流式识别（可运行 voice-setup --refine 升级整句识别）")
    diarizer = None
    segment_model = root / SEGMENTATION_NAME / "model.onnx"
    if segment_model.is_file() and refiner is not None and speakers is not None:
        from .diarization import LocalDiarizer

        def segmenter(model):
            speaker_model = root / SPEAKER_MODELS[model]
            if not speaker_model.is_file():
                raise ValueError(f"声纹模型 {model} 尚未准备")
            cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segment_model)),
                    num_threads=threads, provider="cpu"),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(speaker_model), num_threads=threads, provider="cpu"),
                clustering=sherpa_onnx.FastClusteringConfig(threshold=.5),
            )
            if not cfg.validate():
                raise ValueError("说话人分段模型配置无效")
            return sherpa_onnx.OfflineSpeakerDiarization(cfg)
        model = os.getenv('JSHI_VOICE_SPEAKER_MODEL', 'cam++')
        diarizer = LocalDiarizer(segmenter(model), speakers, refiner, build_engine=segmenter, model=model)
        print("说话人时间线：本地分段，最长 6 秒一个处理窗（不是音源分离）")
    return LocalASR(recognizer, speakers, refiner, diarizer)


TTS_NAME = "vits-melo-tts-zh_en"


def setup_models(data_dir: Path, *, tts: bool = False, refine: bool = False, diarize: bool = False, speaker_model: str = 'cam++') -> None:
    """Fetch explicit, small official CPU models, with safe archive extraction."""
    from urllib.request import urlopen
    import tarfile
    import shutil
    root = Path(os.getenv("JSHI_VOICE_LOCAL_DIR", str(data_dir / "voice_models")))
    root.mkdir(parents=True, exist_ok=True)
    if speaker_model not in SPEAKER_MODELS:
        raise ValueError('unknown speaker model')
    speaker_name = SPEAKER_MODELS[speaker_model]
    assets = [(ASR_NAME + ".tar.bz2", "asr-models"), (speaker_name, "speaker-recongition-models")]
    if tts:
        assets.append((TTS_NAME + ".tar.bz2", "tts-models"))
    if refine or diarize:
        assets.append((REFINER_NAME + ".tar.bz2", "asr-models"))
    if diarize:
        assets.append((SEGMENTATION_NAME + ".tar.bz2", "speaker-segmentation-models"))
    for name, tag in assets:
        destination = root / name
        if (name == speaker_name and destination.is_file()) or (name.endswith(".tar.bz2") and (root / name.removesuffix(".tar.bz2") / "tokens.txt").is_file()):
            continue
        print(f"下载 {name} …", flush=True)
        tmp = root / (name + ".download")
        with urlopen(f"https://github.com/k2-fsa/sherpa-onnx/releases/download/{tag}/{name}", timeout=60) as response, tmp.open("wb") as output:
            shutil.copyfileobj(response, output)
        if name.endswith(".tar.bz2"):
            with tarfile.open(tmp) as archive:
                members = archive.getmembers()
                for member in members:
                    resolved = (root / member.name).resolve()
                    if not resolved.is_relative_to(root.resolve()) or not (member.isfile() or member.isdir()):
                        raise ValueError("unsafe model archive")
                archive.extractall(root, members=members, filter="data")
            tmp.unlink()
        else:
            tmp.replace(destination)
    print(f"本地模型已准备：{root.resolve()}")
