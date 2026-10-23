"""Attach local speaker evidence to externally transcribed audio; no capture or ASR."""
from dataclasses import replace

def match_audio(audio, transcript, speakers, *, pcm=None):
    if not transcript.final or transcript.voiceprint_id:
        return transcript
    if transcript.overlap and transcript.audio_kind != 'separated':
        with speakers.lock:
            speakers.last_embedding.pop(transcript.track_id, None)
        return replace(transcript, identity_uncertain=True, identity_note='声音重叠，不据此确认或新增人物')
    pcm = pcm if pcm is not None else audio.segment(transcript.start_ms, transcript.end_ms)
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
