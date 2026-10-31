import json
from threading import RLock
from types import SimpleNamespace

import pytest

from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.contracts import Transcript


def test_provider_tail_rounding_uses_existing_audio_only():
    audio = SessionAudio()
    audio.append(b'\x01\x02' * 291200)  # 18.200 seconds
    pcm, bounds = audio.segment_with_bounds(15082, 18202)
    assert len(pcm) == (18200 - 15082) * 32
    assert bounds['tail_trim_ms'] == 2
    assert bounds['requested_end_ms'] == 18202
    assert bounds['actual_end_ms'] == 18200
    assert audio.segment(15082, 18220) == pcm
    assert audio.segment(15082, 18221) is None
    assert audio.segment(15082, 18300) is None  # a missing packet is not rounding
    assert audio.segment(-1, 100) is None
    assert audio.segment(18200, 18202) is None


def test_tail_tolerance_does_not_hide_evicted_audio():
    audio = SessionAudio(seconds=1)
    audio.append(b'\x01\x02' * 32000)
    assert audio.segment(500, 2002) is None
    assert len(audio.segment(1000, 2002)) == 32000


def test_matching_retains_requested_time_and_records_adjustment():
    pytest.importorskip('numpy')
    audio = SessionAudio(); audio.append(b'\x01\x02' * 16000)
    calls = []
    class Speakers:
        lock = RLock()
        last_embedding = {}
        binding = {}
        last_match = {'reason': 'test'}
        def identify_timed(self, samples, **kwargs):
            calls.append(len(samples))
            return 'known-person', 'local:person', .8
    original = Transcript('末句', '0', 500, 1002, True)
    matched = audio.match(original, Speakers())
    assert matched.end_ms == 1002 and matched.voiceprint_id == 'local:person'
    assert calls == [8000]
    assert json.loads(matched.identity_note)['audio_boundary']['actual_end_ms'] == 1000


def test_old_local_pipeline_records_window_and_release_timing():
    pytest.importorskip('numpy')
    from jshi.voice.pipeline import LocalASR
    class Stream:
        def accept_waveform(self, *args): pass
    recognizer = SimpleNamespace(create_stream=lambda: Stream(), is_ready=lambda _: False,
        get_result=lambda _: '文字', is_endpoint=lambda _: False, reset=lambda _: None)
    diarizer = SimpleNamespace(transcribe=lambda samples, offset: (
        Transcript('前段', 'A', offset, offset+2000, True),
        Transcript('后段', 'A', offset+3000, offset+6000, True)))
    pipeline = LocalASR(recognizer, diarizer=diarizer)
    assert len(pipeline.feed(b'\0\0' * 96000)) == 2
    assert len(pipeline.last_timings) == 2
    assert all(t['cut_reason'] == 'length_limit' and t['window_end_ms'] == 6000
               and t['analysis_transcription_ms'] >= 0 for t in pipeline.last_timings)
