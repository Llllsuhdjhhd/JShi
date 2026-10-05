from __future__ import annotations

import asyncio

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer

from jshi.app.voice import create_app
from jshi.voice.config import VoiceConfig
from jshi.voice.volc import Transcript
from tests.test_voice import process, FakeCloud


class Local:
    speakers = None
    def reset_session(self): pass
    def feed(self, pcm):
        return (Transcript("你好", "A", 0, 2000, True),)


def test_matching_slider_websocket_applies_and_rejects_invalid_values(process, monkeypatch, tmp_path):
    from jshi.voice.local import LocalSpeakers
    speakers=LocalSpeakers(None,'model',tmp_path/'prints.json',process.profiles)
    local=Local();local.speakers=speakers
    monkeypatch.setattr('jshi.app.voice.VolcVoice',lambda config,http:FakeCloud())
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig('secret'),local=local))) as client:
            async with client.ws_connect('/voice',headers={'Origin':str(client.make_url('/')).rstrip('/')}) as ws:
                assert (await ws.receive_json())['match_threshold']==.65
                await ws.send_json({'type':'voice_settings','match_threshold':.55})
                saved=await ws.receive_json()
                assert saved['type']=='voice_settings' and saved['match_threshold']==.55
                await ws.send_json({'type':'voice_settings','match_threshold':1.5})
                failed=await ws.receive_json()
                assert failed['type']=='voice_settings_error' and failed['match_threshold']==.55
    asyncio.run(run())
    assert LocalSpeakers(None,'model',speakers.path,process.profiles).threshold==.55


def test_comparison_websocket_saves_labeled_audio_and_returns_real_report(process, monkeypatch, tmp_path):
    from threading import RLock
    from types import SimpleNamespace
    class TrialLocal:
        speakers=None
        def __init__(self): self.size=0
        def reset_session(self): self.size=0
        def feed(self, pcm):
            self.size += len(pcm)
            if self.size==160000:return (Transcript('登记音频','A',0,5000,True),)
            if self.size==224000:return (Transcript('独立测试音频','A',5000,7000,True),)
            return ()
    def factory():
        return SimpleNamespace(lock=RLock(),model_id='fixture',threshold=.65,margin=.08,
            embedding=lambda samples:[1.,0.])
    monkeypatch.setattr('jshi.app.voice.VolcVoice',lambda config,http:FakeCloud())
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig('secret',input_pause_s=.3),
                local=TrialLocal(),speaker_models={'cam++':factory,'eres2netv2':factory},trial_root=tmp_path/'trial'))) as client:
            async with client.ws_connect('/voice',headers={'Origin':str(client.make_url('/')).rstrip('/')}) as ws:
                ready=await ws.receive_json()
                assert ready['comparison_available']
                async def until(kind):
                    while True:
                        m=await asyncio.wait_for(ws.receive_json(),4)
                        if m['type']==kind:return m
                for _ in range(5): await ws.send_bytes(b'\1\0'*16000)
                first=await until('timeline')
                await ws.send_json({'type':'compare_add','role':'enroll','name':'lux','input_ids':[first['input_id']]})
                assert len((await until('comparison_samples'))['samples'])==1
                for _ in range(2): await ws.send_bytes(b'\2\0'*16000)
                second=await until('timeline')
                await ws.send_json({'type':'compare_add','role':'test','name':'lux','input_ids':[second['input_id']]})
                assert len((await until('comparison_samples'))['samples'])==2
                await ws.send_json({'type':'compare_run'})
                result=await until('comparison_report')
                assert result['report']['models']['eres2netv2']['enrollment_accepted']==1
                assert result['report']['models']['cam++']['results'][0]['predicted']=='lux'
                assert result['report']['cloud']['status']=='not_run'
    asyncio.run(run())


def test_local_websocket_audio_roundtrip_and_origin_guard(process, monkeypatch):
    monkeypatch.setattr("jshi.app.voice.VolcVoice", lambda config,http: FakeCloud())
    async def run():
        async with TestClient(TestServer(create_app(process,"stone",VoiceConfig("not-for-browser"),local=Local()))) as client:
            page=await client.get("/")
            body=await page.text()
            assert "LARK A2" in body and "not-for-browser" not in body
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect("/voice",headers={"Origin":"https://another-site.example"})
            origin=str(client.make_url("/")).rstrip("/")
            assert not (await (await client.get('/status')).json())['busy']
            async with client.ws_connect("/voice",headers={"Origin":origin}) as ws:
                assert (await ws.receive_json())["type"] == "ready"
                assert (await (await client.get('/status')).json())['busy']
                with pytest.raises(aiohttp.WSServerHandshakeError):
                    await client.ws_connect("/voice",headers={"Origin":origin})
                await ws.send_bytes(b"\0\0"*1600)
                events=[]
                while not any(m["type"]=="audio_end" for m in events):
                    events.append(await asyncio.wait_for(ws.receive_json(),4))
                reply=next(m for m in events if m["type"]=="reply")
                assert any(m["type"]=="transcript" and m["final"] for m in events)
                assert any(m["type"]=="audio" for m in events)
                await ws.send_json({"type":"played","reply_id":reply["reply_id"],"segment":0,"phase":"started"})
                await ws.send_json({"type":"played","reply_id":reply["reply_id"],"segment":0,"phase":"completed"})
                await ws.send_json({"type":"stop"})
                while True:
                    m=await asyncio.wait_for(ws.receive_json(),4)
                    if m["type"]=="stop": break
                history=process.repository.list_history("stone",limit=100)
                assert any(r.event_type=="voice_delivery" and r.content.get("played_text")=="上午去古镇。" for r in history)
    asyncio.run(run())


def test_tts_failure_preserves_text_and_cancels_audio(process, monkeypatch):
    class BrokenCloud(FakeCloud):
        async def synthesize(self,text):
            raise RuntimeError("service unavailable")
            yield b""
    monkeypatch.setattr("jshi.app.voice.VolcVoice",lambda config,http:BrokenCloud())
    async def run():
        async with TestClient(TestServer(create_app(process,"stone",VoiceConfig("secret"),local=Local()))) as client:
            origin=str(client.make_url("/")).rstrip("/")
            async with client.ws_connect("/voice",headers={"Origin":origin}) as ws:
                await ws.receive_json()
                await ws.send_bytes(b"\0\0"*1600)
                events=[]
                while not any(m["type"]=="notice" for m in events):
                    events.append(await asyncio.wait_for(ws.receive_json(),4))
                assert any(m["type"]=="reply" for m in events)
                assert any(m["type"]=="stop" for m in events)
                assert not any(m["type"]=="audio" for m in events)
    asyncio.run(run())


def test_online_selection_requires_key_and_status_does_not_expose_it(process):
    async def run():
        async with TestClient(TestServer(create_app(process, 'stone',
                VoiceConfig('', asr_backend='local', tts_backend='local'), local=Local()))) as client:
            status = await (await client.get('/status')).json()
            assert status['local_available'] and not status['online_available']
            origin = str(client.make_url('/')).rstrip('/')
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await client.ws_connect('/voice?asr=online', headers={'Origin': origin})
            assert exc.value.status == 400
            assert not (await (await client.get('/status')).json())['busy']
    asyncio.run(run())


def test_online_frames_use_cloud_timeline_and_local_registered_identity(process, monkeypatch):
    import gzip
    import json
    import struct
    from threading import RLock
    from types import SimpleNamespace
    from jshi.recognition import CarrierEntry
    process.profiles.add_carrier('lux-id', CarrierEntry('voiceprint', 'local:model:lux'))
    class Speakers:
        lock = RLock()
        tracks = {}
        last_embedding = {}
        binding = {}
        def identify(self, samples):
            assert len(samples) == 32000
            self.last_embedding['known'] = [1., 0.]
            return 'known', 'local:model:lux', .9
    class ASR:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.samples = 0
        def __aiter__(self): return self
        async def __anext__(self): return await self.queue.get()
        async def close(self): pass
        async def send_bytes(self, frame):
            if frame[1] >> 4 != 2: return
            self.samples += len(gzip.decompress(frame[8:])) // 2
            if self.samples == 32000:
                body = gzip.compress(json.dumps({'result': {'utterances': [{
                    'text': '今天聊点什么', 'start_time': 0, 'end_time': 2000,
                    'definite': True, 'additions': {'speaker_id': '0'}}]}}).encode())
                await self.queue.put(SimpleNamespace(type=aiohttp.WSMsgType.BINARY,
                    data=bytes((0x11, 0x90, 0x11, 0)) + struct.pack('>I', len(body)) + body))
    class Cloud(FakeCloud):
        async def open_asr(self): return ASR()
    monkeypatch.setattr('jshi.app.voice.VolcVoice', lambda config,http: Cloud())
    async def run():
        async with TestClient(TestServer(create_app(process, 'stone', VoiceConfig('secret'),
                online_speakers=Speakers()))) as client:
            origin = str(client.make_url('/')).rstrip('/')
            async with client.ws_connect('/voice?asr=online', headers={'Origin': origin}) as ws:
                assert (await ws.receive_json())['type'] == 'ready'
                await ws.send_bytes(b'\0\0' * 16000)
                await ws.send_bytes(b'\0\0' * 16000)
                events = []
                while not any(m['type'] == 'audio_end' for m in events):
                    events.append(await asyncio.wait_for(ws.receive_json(), 3))
                timeline = next(m for m in events if m['type'] == 'timeline')
                assert timeline['speaker']['object_id'] == 'lux-id'
                assert timeline['speaker']['track_id'] == '0'
                assert timeline['speaker']['method'] == 'voiceprint_match'
                assert (timeline['start_ms'], timeline['end_ms']) == (0, 2000)
    asyncio.run(run())
