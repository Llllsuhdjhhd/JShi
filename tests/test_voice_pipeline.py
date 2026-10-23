"""Stage contracts: interchangeable capture, analysis and transcription implementations."""
from types import SimpleNamespace
import wave

import pytest

np = pytest.importorskip('numpy')
from jshi.voice.capture import PcmCapture, WavCapture
from jshi.voice.contracts import AudioFrame, SpeechSegment
from jshi.voice.pipeline import SpeechPipeline, transcribe_capture, transcribe_segments
from jshi.voice.analysis import SpeakerAnalyzer
from jshi.voice.segmentation import SileroSegmenter


def test_file_and_push_capture_share_exact_audio_and_positions(tmp_path):
    pcm = np.arange(4001, dtype='<i2').tobytes()
    path = tmp_path / 'input.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(pcm)
    frames = list(WavCapture(path).frames())
    push = PcmCapture()
    assert frames == [push.accept(pcm[i:i+3200]) for i in range(0, len(pcm), 3200)]
    assert [f.start_sample for f in frames] == [0, 1600, 3200]
    with pytest.raises(ValueError):
        push.accept(b'x')
    assert push.position == 4001


def test_capture_contract_rejects_gaps_and_wrong_format(tmp_path):
    class Source:
        def frames(self):
            return [AudioFrame(b'\0\0', 5)]
    decoder = SimpleNamespace(reset_session=lambda: None)
    with pytest.raises(ValueError, match='不连续'):
        list(transcribe_capture(Source(), decoder))
    path = tmp_path / 'wrong.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
    with pytest.raises(ValueError, match='16kHz'):
        list(WavCapture(path).frames())


def test_pipeline_injects_independent_analysis_and_transcriber():
    class Detector:
        def reset(self): pass
        def feed(self, pcm, final=False):
            return [(np.ones(16000), 4200)] if pcm else []
    class Analyzer:
        speakers = None
        def analyze(self, samples, offset_ms):
            assert len(samples) == 16000 and offset_ms == 4200
            return [SpeechSegment(samples, 'person-A', offset_ms, offset_ms+1000),
                    SpeechSegment(samples, 'person-B', offset_ms+1000, offset_ms+2000)]
    for words in [('甲', '乙'), ('替换模型甲', '替换模型乙')]:
        class Transcriber:
            def transcribe_many(self, audios):
                assert len(audios) == 2
                return words
        pipeline = SpeechPipeline(Detector(), Analyzer(), Transcriber(), backend='test')
        rows = pipeline.feed(b'\0\0')
        assert [(t.track_id, t.text, t.start_ms) for t in rows] == [
            ('person-A', words[0], 4200), ('person-B', words[1], 5200)]
        assert pipeline.feed(b'', final=True) == ()
    with pytest.raises(ValueError, match='数量'):
        transcribe_segments([SpeechSegment([], 'A', 0, 1)], SimpleNamespace(transcribe_many=lambda _: []))


def test_analysis_runs_without_any_text_model_and_preserves_overlap():
    spans = [SimpleNamespace(start=0, end=2, speaker=0), SimpleNamespace(start=1, end=3, speaker=1)]
    engine = SimpleNamespace(process=lambda _: SimpleNamespace(sort_by_start_time=lambda: spans))
    speakers = SimpleNamespace(identify=lambda _: pytest.fail('mixed audio must not identify'))
    rows = SpeakerAnalyzer(engine, speakers).analyze(np.zeros(48000), 8000)
    assert [(r.start_ms, r.end_ms, r.overlap) for r in rows] == [(8000, 10000, True), (9000, 11000, True)]
    assert all(not r.voiceprint_id for r in rows)


def test_alternate_embedding_model_uses_shared_contract():
    from jshi.voice.features import speaker_features
    assert speaker_features(SimpleNamespace(embed=lambda _: [0.2, 0.8]), np.zeros(24000)) == [0.2, 0.8]
    assert speaker_features(SimpleNamespace(embed=lambda _: [float('nan')]), np.zeros(24000)) is None


def test_vad_flush_keeps_real_sample_positions_and_does_not_invent_padding():
    class Detector:
        def __init__(self): self.queue = []
        def accept_waveform(self, samples): assert len(samples) == 512
        def empty(self): return not self.queue
        def flush(self): self.queue.append(SimpleNamespace(start=100, samples=np.ones(924)))
        @property
        def front(self): return self.queue[0]
        def pop(self): self.queue.pop(0)
        def reset(self): self.queue.clear()
    vad = SileroSegmenter(Detector())
    assert vad.feed(b'\0\0' * 700) == ()
    result = vad.feed(b'', final=True)
    assert len(result) == 1 and len(result[0][0]) == 600 and result[0][1] == 6
    vad.reset()
    assert vad.total == 0 and len(vad.pending) == 0
