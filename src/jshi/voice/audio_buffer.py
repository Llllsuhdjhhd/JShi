"""Bounded session-relative PCM for matching cloud utterances locally."""
from dataclasses import replace
from threading import RLock
from time import time


class SessionAudio:
    def __init__(self, seconds=45):
        self.limit = int(seconds * 32000)  # mono 16 kHz int16
        self.data = bytearray()
        self.offset = 0
        self.lock = RLock()
        self.started_at_ms = None

    def append(self, pcm):
        with self.lock:
            if self.started_at_ms is None and pcm:
                self.started_at_ms = time() * 1000 - len(pcm) / 32
            self.data.extend(pcm)
            excess = max(0, len(self.data) - self.limit)
            if excess:
                del self.data[:excess]
                self.offset += excess

    def segment(self, start_ms, end_ms):
        with self.lock:
            start, end = start_ms * 32 - self.offset, end_ms * 32 - self.offset
            if start < 0 or end > len(self.data) or end <= start:
                return None
            return bytes(self.data[start:end])

    def match(self, transcript, speakers):
        if not transcript.final or transcript.voiceprint_id:
            return transcript
        if transcript.overlap:
            with speakers.lock:
                speakers.last_embedding.pop(transcript.track_id, None)
            return replace(transcript, identity_uncertain=True, identity_note='声音重叠，不据此确认或新增人物')
        pcm = self.segment(transcript.start_ms, transcript.end_ms)
        if pcm is None:
            with speakers.lock:
                speakers.last_embedding.pop(transcript.track_id, None)
            return replace(transcript, identity_uncertain=True, identity_note='对应音频不在缓存内，未做声纹比对')
        import numpy as np
        samples = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
        if hasattr(speakers, 'identify_timed'):
            track, vpid, score = speakers.identify_timed(samples, source_track=transcript.track_id,
                start_ms=transcript.start_ms, end_ms=transcript.end_ms)
        else:
            track, vpid, score = speakers.identify(samples)
        # Keep the cloud timeline track; the embedding belongs to that same track
        # for a later explicit introduction. Anonymous local clusters aren't ids.
        with speakers.lock:
            vector = speakers.last_embedding.get(track)
            if vector is not None:
                speakers.last_embedding[transcript.track_id] = vector
            else:
                speakers.last_embedding.pop(transcript.track_id, None)
            getattr(speakers, 'binding', {}).pop(transcript.track_id, None)
        cluster = track if not track.startswith('unidentified-') else ''
        import json
        return replace(transcript, voiceprint_id=vpid, confidence=score if vpid else None, speaker_cluster_id=cluster,
            identity_tentative=bool(cluster and not vpid and getattr(speakers, 'last_match', {}).get('tentative', True)),
            identity_note=json.dumps(getattr(speakers, 'last_match', {}), ensure_ascii=False), identity_uncertain=not bool(cluster or vpid))
