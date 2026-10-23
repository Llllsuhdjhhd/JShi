import base64
import io
from threading import Thread
from types import SimpleNamespace
import wave

import pytest

np = pytest.importorskip('numpy')
from jshi.voice.config import VoiceConfig
from jshi.voice.qwen import QwenRecognizer
from jshi.voice.qwen_batch_server import BatchTranscription, make_server


def wav_url(value=100):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(np.full(1600, value, dtype='<i2').tobytes())
    return 'data:audio/wav;base64,' + base64.b64encode(buffer.getvalue()).decode()


def test_one_http_batch_is_one_native_model_call_and_preserves_all_audio():
    calls = []
    class Model:
        def transcribe(self, *, audio, language, return_time_stamps):
            calls.append(audio)
            assert language is None and return_time_stamps is False
            return [SimpleNamespace(text=f'音频{round(float(samples[0])*32768)}', language='Chinese')
                    for samples, rate in audio if rate == 16000]
    service = BatchTranscription(Model(), 'served-model', max_batch_size=8)
    server = make_server(('127.0.0.1', 0), service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = VoiceConfig('', asr_backend='qwen', tts_backend='local',
            qwen_asr_url=f'http://127.0.0.1:{server.server_port}/v1')
        recognizer = QwenRecognizer(config)
        recognizer.check()
        audios = [np.full(1600, v/32768, dtype='float32') for v in (100, 200, 300)]
        assert recognizer.transcribe_many(audios) == ['音频100', '音频200', '音频300']
        assert len(calls) == 1 and len(calls[0]) == 3
        assert recognizer.last_timing['http_requests'] == 1
        assert recognizer.last_timing['http_batch_ms'] >= 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def checked_recognizer(monkeypatch, *, size=8):
    r = QwenRecognizer(VoiceConfig('', asr_backend='qwen', tts_backend='local', qwen_asr_batch_size=size))
    monkeypatch.setattr(r, '_request', lambda *_: {'data': [{'id': 'model', 'capabilities': {'audio_batch': True, 'max_batch_size': 8}}]})
    r.check()
    return r


def test_batch_matches_short_ids_even_when_results_are_reordered(monkeypatch):
    r = checked_recognizer(monkeypatch)
    calls = []
    def request(path, body):
        calls.append(body)
        assert path == '/asr/batch' and set(body) == {'model', 'items'}
        assert [i['id'] for i in body['items']] == ['s1', 's2']
        return {'results': [{'id': 's2', 'text': '乙'}, {'id': 's1', 'text': '甲'}]}
    monkeypatch.setattr(r, '_request', request)
    assert r.transcribe_pcm_many([b'\0\0', b'', b'\1\0']) == ['甲', '', '乙']
    assert len(calls) == 1


def test_oversized_batch_uses_sequential_small_batches(monkeypatch):
    r = checked_recognizer(monkeypatch, size=2)
    sizes = []
    def request(path, body):
        sizes.append(len(body['items']))
        return {'results': [{'id': i['id'], 'text': '文字'} for i in body['items']]}
    monkeypatch.setattr(r, '_request', request)
    assert len(r.transcribe_pcm_many([b'\0\0'] * 5)) == 5
    assert sizes == [2, 2, 1]
    assert r.last_timing['http_requests'] == 3


@pytest.mark.parametrize('response', [{}, {'results': []}, {'results': [{'id': 's1', 'text': None}]},
    {'results': [{'id': 'other', 'text': '错配'}]}])
def test_qwen_rejects_incomplete_or_mismatched_batches(response, monkeypatch):
    recognizer = checked_recognizer(monkeypatch)
    monkeypatch.setattr(recognizer, '_request', lambda *_: response)
    with pytest.raises(RuntimeError, match='批量返回'):
        recognizer.transcribe_pcm(b'\0\0' * 1600)


def test_qwen_rejects_standard_service_and_ambiguous_model(monkeypatch):
    recognizer = QwenRecognizer(VoiceConfig('', asr_backend='qwen', tts_backend='local'))
    monkeypatch.setattr(recognizer, '_request', lambda *_: {'data': [{'id': 'one'}, {'id': 'two'}]})
    with pytest.raises(ValueError, match='不唯一'):
        recognizer.check()
    monkeypatch.setattr(recognizer, '_request', lambda *_: {'data': [{'id': 'one'}]})
    with pytest.raises(ValueError, match='不支持批转写'):
        recognizer.check()


def test_server_validates_entire_batch_before_inference():
    model = SimpleNamespace(transcribe=lambda **_: pytest.fail('invalid batch reached inference'))
    service = BatchTranscription(model, 'model', max_batch_size=2)
    for items in ([{'id': 's1', 'audio_url': wav_url()}, {'id': 's1', 'audio_url': wav_url()}],
                  [{'id': 's1', 'audio_url': 'https://example.test/input.wav'}],
                  [{'id': 's1', 'audio_url': 'data:audio/wav;base64,not-wav'}], []):
        with pytest.raises(ValueError):
            service.transcribe({'model': 'model', 'items': items})


def test_qwen_env_does_not_require_cloud_credentials(monkeypatch):
    monkeypatch.setenv('JSHI_VOICE_API_KEY', '')
    monkeypatch.setenv('JSHI_VOICE_ASR', 'qwen')
    monkeypatch.setenv('JSHI_VOICE_TTS', 'local')
    assert VoiceConfig.from_env().asr_backend == 'qwen'
    with pytest.raises(ValueError):
        VoiceConfig('', qwen_asr_batch_size=0)
