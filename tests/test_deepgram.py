import asyncio
import pytest
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from jshi.voice.config import VoiceConfig
from jshi.voice.deepgram import DeepgramASR, DeepgramAssembler
from jshi.app.voice import create_app
from tests.test_voice import process


def result(final=True, speaker=0):
    return {'type':'Results','is_final':final,'speech_final':False,'start':1,'duration':1,
        'channel':{'alternatives':[{'transcript':'你好，匠石。','words':[
            {'word':'你好','punctuated_word':'你好，','start':1,'end':1.3,'speaker':speaker},
            {'word':'匠石','punctuated_word':'匠石。','start':1.4,'end':2,'speaker':speaker}]}]}}


def test_deepgram_configuration_and_independent_keys(monkeypatch):
    monkeypatch.setenv('JSHI_VOICE_API_KEY','')
    monkeypatch.setenv('JSHI_DEEPGRAM_API_KEY','test-secret')
    monkeypatch.setenv('JSHI_VOICE_ASR','deepgram')
    monkeypatch.setenv('JSHI_VOICE_TTS','local')
    config = VoiceConfig.from_env()
    assert config.asr_backend == 'deepgram' and config.api_key == ''
    assert config.deepgram_language == 'zh-CN' and config.deepgram_model == 'nova-3'
    assert 'test-secret' not in repr(config)
    with pytest.raises(ValueError,match='火山'):
        VoiceConfig.from_env(tts='online')
    monkeypatch.setenv('JSHI_DEEPGRAM_DIARIZE','off')
    assert not VoiceConfig.from_env().deepgram_diarize
    monkeypatch.setenv('JSHI_DEEPGRAM_API_KEY','')
    with pytest.raises(ValueError,match='JSHI_DEEPGRAM_API_KEY'):
        VoiceConfig.from_env()
    with pytest.raises(ValueError) as error:
        VoiceConfig.from_env(tts='online')
    assert '播报（TTS）' in str(error.value)
    assert 'JSHI_VOICE_API_KEY' in str(error.value) and 'JSHI_DEEPGRAM_API_KEY' in str(error.value)


def test_deepgram_partial_final_dedup_and_anonymous_speakers():
    assembler = DeepgramAssembler()
    assert not assembler.accept(result(False))[0].final
    final = assembler.accept(result())[0]
    assert final.final and final.start_ms == 1000 and final.end_ms == 2000
    assert final.voiceprint_id == '' and final.track_id == 'deepgram-0'
    assert assembler.accept(result(speaker=1)) == ()  # late label revision is not new speech
    other = result()
    other['channel']['alternatives'][0]['words'][1]['speaker'] = 1
    split = DeepgramAssembler().accept(other)
    assert [t.track_id for t in split] == ['deepgram-0','deepgram-1']
    assert [t.text for t in split] == ['你好，','匠石。']
    missing = result()
    for word in missing['channel']['alternatives'][0]['words']:
        word.pop('speaker')
    assert DeepgramAssembler().accept(missing)[0].track_id.startswith('unidentified-')
    assert assembler.accept({'type':'Metadata'}) == ()
    with pytest.raises(RuntimeError,match='invalid'):
        assembler.accept({'type':'Error','description':'invalid'})


def test_deepgram_handshake_raw_audio_and_final_flush(process, monkeypatch):
    captured = {}
    async def upstream(request):
        captured['auth'] = request.headers['Authorization']
        captured['query'] = dict(request.query)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                captured['audio'] = message.data
                await ws.send_json(result(False))
            elif message.type == WSMsgType.TEXT and message.json()['type'] == 'CloseStream':
                captured['closed'] = True
                await ws.send_json(result())
                await ws.send_json({'type':'Metadata'})
                break
        await ws.close()
        return ws

    async def run():
        fake = web.Application()
        fake.router.add_get('/listen',upstream)
        async with TestServer(fake) as server:
            original = DeepgramASR.open_asr
            async def local_open(adapter):
                class Redirect:
                    async def ws_connect(self,url,**kwargs):
                        assert url == 'wss://api.deepgram.com/v1/listen'
                        return await adapter.http.ws_connect(server.make_url('/listen'),**kwargs)
                return await original(DeepgramASR(adapter.config,Redirect()))
            monkeypatch.setattr(DeepgramASR,'open_asr',local_open)
            config = VoiceConfig('',asr_backend='deepgram',deepgram_api_key='mock-secret')
            app = create_app(process,'stone',config)
            async with TestClient(TestServer(app)) as client:
                status = await (await client.get('/status')).json()
                assert status['asr'] == 'deepgram' and status['deepgram_available']
                assert 'mock-secret' not in str(status)
                ws = await client.ws_connect('/voice',origin=str(client.make_url('')).rstrip('/'))
                ready = await ws.receive_json()
                assert ready['type'] == 'ready' and 'Deepgram' in ready['asr']
                await ws.send_bytes(b'\x00\x01'*1600)
                while True:
                    event = await ws.receive_json(timeout=5)
                    if event['type'] == 'transcript':
                        break
                await ws.send_json({'type':'end'})
                events = []
                async for message in ws:
                    if message.type == WSMsgType.TEXT:
                        events.append(message.json())
                assert any(e['type']=='transcript' and e.get('final') for e in events)
                assert not any(e['type']=='notice' and '失败' in e.get('text','') for e in events)
        assert captured['auth'] == 'Token mock-secret'
        assert captured['query']['encoding'] == 'linear16'
        assert captured['query']['language'] == 'zh-CN'
        assert captured['query']['diarize_model'] == 'v1'
        assert captured['audio'] == b'\x00\x01'*1600 and captured['closed']
    asyncio.run(run())
