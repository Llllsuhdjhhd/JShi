import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from types import SimpleNamespace

import numpy as np

from jshi.app.voice import VoiceConversation
from jshi.voice.contracts import SpeechSegment, Transcript
from jshi.voice.latency import AudioReceiptClock, append_latency, public_timing
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.local_input import LocalAudioPump
from jshi.voice.pipeline import SpeechPipeline
from jshi.voice.segmentation import SileroSegmenter
from tests.test_voice import process, FakeCloud


def test_receipt_clock_uses_packet_arrival_not_audio_elapsed_wall_time():
    clock = AudioReceiptClock()
    clock.append(b'\0' * 3200, at=10)
    clock.append(b'\0' * 3200, at=15)  # delayed packet, not synthetic +100ms
    assert clock.at_ms(50) == 10
    assert clock.at_ms(100) == 15
    assert clock.at_ms(200) is None
    timing = public_timing({'_detected_mono':11, '_asr_return_mono':15,
                            '_text_sent_mono':17, '_audio_end_received_mono':14})
    assert timing['detected_to_text_sent_ms'] == 6000
    assert timing['audio_end_received_to_text_sent_ms'] == 3000
    assert not any(k.startswith('_') for k in timing)
    assert public_timing({})['detected_to_text_sent_ms'] is None


def test_vad_observes_real_onset_and_endpoint_without_changing_spans():
    class Detector:
        def __init__(self): self.queue = []; self.n = 0
        def accept_waveform(self, samples):
            self.n += 1
            if self.n == 3:
                self.queue.append(SimpleNamespace(start=0, samples=np.ones(1024)))
        def is_speech_detected(self): return self.n < 3
        def empty(self): return not self.queue
        @property
        def front(self): return self.queue[0]
        def pop(self): self.queue.pop(0)
        def reset(self): self.n = 0; self.queue.clear()
    vad = SileroSegmenter(Detector())
    assert not vad.feed(b'\0\0' * 512)
    detected = vad.onset
    assert detected is not None
    rows = vad.feed(b'\0\0' * 1024)
    assert len(rows[0][0]) == 1024 and rows[0][1] == 0
    assert vad.last_windows[0]['_detected_mono'] == detected
    assert vad.last_windows[0]['endpoint_audio_ms'] == 96
    vad.reset()
    assert vad.onset is None and vad.processed == 0


def test_pipeline_and_pump_preserve_batch_and_publish_separate_diagnostics():
    async def run():
        class Detector:
            last_windows = []
            def feed(self, pcm, final=False):
                if not pcm: return ()
                now = monotonic()
                self.last_windows = [{'start_ms':0,'end_ms':100,'endpoint_audio_ms':100,
                    '_detected_mono':now, '_window_ready_mono':now}]
                return ((np.zeros(1600), 0),)
        class Analyzer:
            speakers = None
            def analyze(self, samples, start):
                return [SpeechSegment(samples[:800], 'A',0,50), SpeechSegment(samples[800:],'B',50,100)]
        class Transcriber:
            last_timing = {'http_batch_ms':5, 'http_requests':1}
            def transcribe_many(self, audios): return ['甲','乙']
        observed, batches = [], []
        async def accept(t): raise AssertionError('whole batch callback required')
        async def batch(rows): batches.append(rows)
        pipeline = SpeechPipeline(Detector(), Analyzer(), Transcriber(), backend='qwen')
        with ThreadPoolExecutor(max_workers=1) as executor:
            pump = LocalAudioPump(pipeline, executor, accept, accept_batch=batch,
                on_timings=lambda rows,timings: observed.extend(dict(t) for t in timings))
            pump.feed(b'\0\0' * 1600)
            await pump.finish()
        assert len(batches[0]) == 2 and len(observed) == 2
        assert all(t['http_requests'] == 1 and t['batch_segments'] == 2 for t in observed)
        assert all(t['analysis_batch_ms'] >= 0 and t['endpoint_backlog_ms'] >= 0 for t in observed)
        assert all(t['_audio_end_received_mono'] is not None for t in observed)
    asyncio.run(run())


def test_text_display_ack_is_persisted_once_without_model_call(process):
    async def run():
        sent = []
        async def send(message): sent.append(message)
        c = VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            c.input_records['test'] = {'text_sent_mono':monotonic(),
                'asr_timing':{'_detected_mono':monotonic()-2,'_asr_return_mono':monotonic()-1}}
            await c.transcript_displayed('unknown', 10)
            await c.transcript_displayed('test', 12)
            await c.transcript_displayed('test', 12)
            rows = [json.loads(line) for line in (process.repository.path.parent/'voice_latency.jsonl').read_text(encoding='utf-8').splitlines()]
            assert len(rows) == 1 and rows[0]['stage'] == 'browser_display_ack'
            assert rows[0]['detected_to_display_ack_upper_ms'] >= 2000
            assert rows[0]['browser_render_ms'] == 12
            assert len(sent) == 1 and sent[0]['type'] == 'voice_latency'
            assert not process.cognition.requests
        finally: await c.close()
    asyncio.run(run())


def test_trace_log_rotates(tmp_path):
    path = tmp_path/'voice_latency.jsonl'
    path.write_bytes(b' ' * (10*1024*1024+1))
    append_latency(path, {'stage':'text_sent'})
    assert path.with_suffix('.previous.jsonl').exists()
    assert json.loads(path.read_text())['stage'] == 'text_sent'


def test_capture_pause_does_not_become_current_sentence_latency():
    audio = SessionAudio(seconds=.1)
    audio.append(b'\1\0' * 1600, received_at_ms=100000, received_mono=100)
    audio.append(b'\2\0' * 1600, received_at_ms=149200, received_mono=149.2)
    # Exact endpoint selects the preceding packet, even after a long capture pause.
    receipt = audio.receipt_timing(100, 200)
    assert receipt['audio_end_received_at_ms'] == 149200
    assert audio.receipt_timing(0, 100)['audio_end_received_at_ms'] == 100000
    assert audio.receipt_timing(200, 201)['audio_end_received_at_ms'] is None
    timing = public_timing({**receipt, '_result_ready_mono':149.4, '_text_sent_mono':149.5})
    assert timing['audio_end_received_to_result_ready_ms'] == 200
    assert timing['audio_end_received_to_text_sent_ms'] == 300
    assert timing['result_ready_to_text_sent_ms'] == 100
    assert audio.clock_drift_ms(now=149.2) == 49100
    assert audio.segment(100, 200) == b'\2\0' * 1600
    # Evicted arrival anchors cannot be reconstructed from sample count.
    audio.receipt_wall.packets.clear()
    audio.receipt_mono.packets.clear()
    assert audio.receipt_timing(100, 200)['audio_end_received_at_ms'] is None


def test_cloud_accept_records_receipt_before_emit_and_keeps_unknown_unknown(process, monkeypatch):
    async def run():
        wall = [200.0]
        monkeypatch.setattr('jshi.app.voice.time', lambda: wall[0])
        async def send(message):
            if message['type'] == 'transcript': wall[0] += 10
        audio = SessionAudio()
        audio.append(b'\0' * 3200, received_at_ms=150000, received_mono=monotonic()-50)
        audio.append(b'\0' * 3200, received_at_ms=199800, received_mono=monotonic()-.2)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, sample_source=audio)
        try:
            await c.accept(Transcript('你好', 'A', 100, 200, True))
            row = next(iter(c.input_records.values()))
            assert row['received_at_ms'] == 200000  # emit's delay did not move the anchor
            assert row['pipeline']['audio_end_received_at_ms'] == 199800
            assert 'speech_end_at_ms' not in row['pipeline']
            timing = public_timing(row['asr_timing'])
            assert 0 <= timing['audio_end_received_to_result_ready_ms'] < 2000
            assert not process.cognition.requests
        finally:
            await c.close()
        c = VoiceConversation(process, 'stone', FakeCloud(), send)
        try:
            await c.accept(Transcript('你好', 'A', 0, 100, True))
            row = next(iter(c.input_records.values()))
            assert public_timing(row['asr_timing'])['audio_end_received_to_result_ready_ms'] is None
        finally:
            await c.close()
    asyncio.run(run())


def test_websocket_timeline_and_display_ack_link_to_same_input(process, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    from tests.test_voice_transport import Local
    monkeypatch.setattr('jshi.app.voice.VolcVoice', lambda config,http: FakeCloud())
    async def run():
        config = VoiceConfig('', asr_backend='local', tts_backend='local')
        async with TestClient(TestServer(create_app(process,'stone',config,
                local=Local(),local_tts=FakeCloud()))) as client:
            async with client.ws_connect('/voice',headers={'Origin':str(client.make_url('/')).rstrip('/')}) as ws:
                assert (await ws.receive_json())['type'] == 'ready'
                await ws.send_bytes(b'\0\0' * 1600)
                while True:
                    event = await asyncio.wait_for(ws.receive_json(),4)
                    if event['type'] == 'timeline': break
                iid = event['input_id']
                assert event['voice_latency']['asr_return_to_text_sent_ms'] >= 0
                assert event['voice_latency']['detected_to_text_sent_ms'] is None
                clock = event['pipeline']
                assert clock['asr_returned_at_ms'] and clock['text_sent_at_ms'] >= clock['asr_returned_at_ms']
                assert clock.get('previous_settled_at_ms')
                await ws.send_json({'type':'transcript_displayed','input_id':iid,'render_ms':16})
                while True:
                    event = await asyncio.wait_for(ws.receive_json(),4)
                    if event['type'] == 'voice_latency': break
                assert event['input_id'] == iid
                assert event['timing']['browser_render_ms'] == 16
                assert event['timing']['text_send_to_display_ack_upper_ms'] >= 0
    asyncio.run(run())
