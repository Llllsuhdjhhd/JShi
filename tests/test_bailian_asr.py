import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest
from aiohttp import WSMsgType
from jshi.voice.config import VoiceConfig
from jshi.voice.bailian import BailianAssembler
from jshi.voice.streaming_asr import open_stream


def event(kind, sentence=None, task='task'):
    return {'header': {'event': kind, 'task_id': task},
            'payload': {'output': {'sentence': sentence or {}}}}


def test_config_has_separate_key_and_no_secret_repr(monkeypatch):
    monkeypatch.setenv('JSHI_VOICE_API_KEY', '')
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'private-key')
    monkeypatch.setenv('JSHI_BAILIAN_ASR_API_KEY', '')
    config = VoiceConfig.from_env(asr='bailian', tts='local')
    assert config.bailian_asr_model == 'qwen-audio-3.1-asr-flash-streaming'
    assert 'private-key' not in repr(config)
    monkeypatch.setenv('DASHSCOPE_API_KEY', '')
    with pytest.raises(ValueError, match='DASHSCOPE_API_KEY'):
        VoiceConfig.from_env(asr='bailian', tts='local')
    with pytest.raises(ValueError, match='wss'):
        replace(config, bailian_asr_url='https://example.com')


def test_preview_final_revision_and_no_invented_speaker():
    a = BailianAssembler()
    sentence = {'sentence_id': 1, 'begin_time': 100, 'end_time': 1000, 'text': '声文', 'sentence_end': False}
    assert not a.accept(event('result-generated', sentence))[0].final
    final = a.accept(event('result-generated', {**sentence, 'sentence_end': True}))[0]
    assert final.track_id.startswith('unidentified-') and not final.voiceprint_id
    revised = a.accept(event('result-generated', {**sentence, 'sentence_end': True, 'text': '声纹'}))[0]
    assert revised.replaces == ((100, 1000),)
    assert not a.accept(event('result-generated', {**sentence, 'sentence_end': True, 'text': '声纹'}))
    assert not a.accept(event('result-generated', {**sentence, 'heartbeat': True}))
    with pytest.raises(RuntimeError, match='FAILED'):
        a.accept({'header': {'event': 'task-failed', 'error_code': 'FAILED'}})


def test_stream_interface_waits_for_ack_and_flushes_last_sentence():
    class Message:
        type = WSMsgType.TEXT
        def __init__(self, data): self.data = data
        def json(self): return self.data
    class Socket:
        close_code = 1000
        def __init__(self): self.sent = []; self.closed = False
        async def send_json(self, data):
            self.sent.append(data)
            self.task = data['header']['task_id']
        async def receive(self): return Message(event('task-started', task=self.task))
        async def send_bytes(self, data): self.sent.append(data)
        async def close(self): self.closed = True
        def exception(self): return None
        def __aiter__(self): return self.events()
        async def events(self):
            yield Message(event('result-generated', {'text': '最后一句', 'begin_time': 0, 'end_time': 500, 'sentence_end': True}, self.task))
            yield Message(event('task-finished', task=self.task))
    class HTTP:
        def __init__(self): self.ws = Socket()
        async def ws_connect(self, url, **kwargs):
            self.headers = kwargs['headers']; return self.ws
    async def run():
        http = HTTP()
        config = VoiceConfig('', asr_backend='bailian', tts_backend='local', bailian_asr_api_key='test-key')
        session = await open_stream(config, http, None)
        assert http.headers['Authorization'] == 'Bearer test-key'
        assert http.ws.sent[0]['payload']['model'] == config.bailian_asr_model
        assert http.ws.sent[0]['payload']['parameters']['vocabulary']['匠石'] == 3
        assert http.ws.sent[0]['payload']['parameters']['max_sentence_silence'] == config.end_window_ms
        assert http.ws.sent[0]['payload']['parameters']['multi_threshold_mode_enabled'] is True
        await session.send_audio(b'\x00\x00')
        await session.finish()
        assert http.ws.sent[-1]['header']['action'] == 'finish-task'
        assert http.ws.sent[-1]['header']['task_id'] == http.ws.sent[0]['header']['task_id']
        outputs = [row async for row in session.results()]
        assert outputs[0][0][0].text == '最后一句' and outputs[-1][1]
        assert session.wait_for_final
        await session.close()
        assert http.ws.closed
    asyncio.run(run())


def test_word_times_are_kept_for_speaker_alignment():
    a = BailianAssembler()
    sentence = {'text': '你好。谁呀？', 'begin_time': 0, 'end_time': 1200, 'sentence_end': True,
                'words': [{'text':'你好','punctuation':'。','begin_time':0,'end_time':500},
                          {'text':'谁呀','punctuation':'？','begin_time':700,'end_time':1200}]}
    result = a.accept(event('result-generated', sentence))[0]
    assert [w.text for w in result.words] == ['你好。', '谁呀？']
    assert [(w.start_ms,w.end_ms) for w in result.words] == [(0,500),(700,1200)]
