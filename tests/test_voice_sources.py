import asyncio
from types import SimpleNamespace

import pytest

from jshi.voice.sources import SpeakerAudioPreparer, SpeakerSource
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.volc import Transcript
from jshi.app.voice import VoiceConversation
from tests.test_voice import process, FakeCloud


def test_solo_and_nonoverlapping_people_share_original_audio_path():
    audio = SessionAudio(); audio.append(b'\x01\x00'*32000)
    async def run():
        preparer = SpeakerAudioPreparer()
        try:
            for count in (1, 4):
                batch = [Transcript(str(i), str(i), i*400, (i+1)*400, True) for i in range(count)]
                result = await preparer.prepare(batch, audio)
                assert len(result) == count
                assert all(kind == 'original' and len(pcm) == 400*32 for _, pcm, kind in result)
        finally: preparer.close()
    asyncio.run(run())


def test_all_sources_get_their_own_audio_text_and_original_timestamps():
    class Separator:
        def separate(self, pcm):
            return [SpeakerSource(str(i), bytes([i, 0])*(len(pcm)//2)) for i in range(1, 5)]
    async def transcribe(pcm):
        return [Transcript('本人文字'+str(pcm[0]), 'cloud-0', 100, 800, True)]
    async def run():
        audio = SessionAudio(); audio.append(b'\0\0'*32000)
        preparer = SpeakerAudioPreparer(Separator(), transcribe)
        try:
            rows = await preparer.prepare([Transcript('原混合文字', 'a', 500, 1500, True, overlap=True)], audio)
            assert len(rows) == 4 and len({t.track_id for t, _, _ in rows}) == 4
            for i, (t, pcm, kind) in enumerate(rows, 1):
                assert t.text == '本人文字'+str(i) and pcm[0] == i
                assert (t.start_ms, t.end_ms) == (600, 1300)
                assert kind == t.audio_kind == 'separated' and t.overlap
            next_rows = await preparer.prepare([Transcript('再来', 'a', 500, 1500, True, overlap=True)], audio)
            assert not {t.track_id for t,_,_ in rows} & {t.track_id for t,_,_ in next_rows}
        finally: preparer.close()
    asyncio.run(run())


def test_separation_failure_never_partially_replaces_originals(process):
    class Separator:
        def separate(self, pcm):
            return [SpeakerSource('a', pcm), SpeakerSource('b', pcm)]
    calls = []
    async def transcribe(pcm):
        calls.append(1)
        if len(calls) == 2: raise RuntimeError('second source failed')
        return [Transcript('只成功一人', 'a', 0, 1000, True)]
    async def run():
        audio = SessionAudio();audio.append(b'\0\0'*16000)
        c = VoiceConversation(process, 'stone', FakeCloud(), lambda m: asyncio.sleep(0),
            sample_source=audio, audio_preparer=SpeakerAudioPreparer(Separator(), transcribe))
        t = Transcript('混合原话', 'a', 0, 1000, True, overlap=True, input_id='parent')
        c.input_records['parent'] = {'transcript':t, 'pcm':audio.segment(0,1000)}
        try:
            fallback = await c._prepare_speaker_audio([t])
            assert len(fallback) == 1 and fallback[0].input_id == 'parent'
            assert fallback[0].text == t.text and fallback[0].audio_kind == 'mixed'
            assert list(c.input_records) == ['parent']
            assert 'superseded_by' not in c.input_records['parent']
        finally: await c.close()
    asyncio.run(run())


def test_separated_record_uses_source_pcm_and_preserves_parent_links(process):
    class Separator:
        def separate(self, pcm): return [SpeakerSource(str(i), bytes([i,0])*16000) for i in range(1,4)]
    async def transcribe(pcm): return [Transcript(str(pcm[0]), 'cloud', 0, 1000, True)]
    async def run():
        audio = SessionAudio();audio.append(b'\0\0'*16000)
        c = VoiceConversation(process, 'stone', FakeCloud(), lambda m: asyncio.sleep(0),
            sample_source=audio, audio_preparer=SpeakerAudioPreparer(Separator(), transcribe))
        t = Transcript('混合', 'a', 0, 1000, True, overlap=True, input_id='p')
        c.input_records['p'] = {'transcript':t,'pcm':audio.segment(0,1000),'at':0}
        try:
            rows = await c._prepare_speaker_audio([t])
            assert len(rows) == 3
            assert c.input_records['p']['superseded_by'] == [u.input_id for u in rows]
            for t in rows:
                row = c.input_records[t.input_id]
                assert row['pcm'][0] == int(t.text) and row['source_inputs'] == ['p']
            await c._revise_transcript(Transcript('混合通道修订', 'a', 0, 1000, True, replaces=((0,1000),)))
            assert {c.input_records[t.input_id]['transcript'].text for t in rows} == {'1','2','3'}
            assert not any(c.input_records[t.input_id].get('superseded_by') for t in rows)
        finally: await c.close()
    asyncio.run(run())


def test_worker_drains_original_queue_count_after_one_to_many_expansion(process):
    class Separator:
        def separate(self, pcm): return [SpeakerSource(str(i), bytes([i,0])*16000) for i in range(1,4)]
    async def transcribe(pcm): return [Transcript('你好'+str(pcm[0]), 'cloud', 0, 1000, True)]
    async def run():
        audio=SessionAudio();audio.append(b'\0\0'*16000)
        events=[]
        async def send(m): events.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send,sample_source=audio,input_pause_s=.01,
            audio_preparer=SpeakerAudioPreparer(Separator(),transcribe))
        try:
            await c.accept(Transcript('大家好', 'a', 0, 1000, True, overlap=True))
            await asyncio.wait_for(c.queue.join(),3)
            rows=[m for m in events if m['type']=='timeline']
            assert len(rows)==3 and {m['text'] for m in rows}=={'你好1','你好2','你好3'}
            assert not c.worker.done()
            assert not any(m['type']=='notice' and '轮次失败' in m.get('text','') for m in events), [m for m in events if m['type']=='notice']
        finally: await c.close()
    asyncio.run(run())


def test_source_asr_uses_own_audio_and_applies_final_text_revision(monkeypatch):
    from aiohttp import WSMsgType
    from jshi.voice.volc import VolcVoice, unpack_asr
    from jshi.voice.config import VoiceConfig
    import jshi.voice.volc as module
    class WS:
        def __init__(self): self.sent=[];self.closed=False
        async def send_bytes(self, data): self.sent.append(data)
        async def close(self): self.closed=True
        def __aiter__(self): return self.responses()
        async def responses(self):
            for text, final in [('陆光',False),('陆广',True)]:
                yield SimpleNamespace(type=WSMsgType.BINARY,data=({'result':{'utterances':[
                    {'text':text,'start_time':0,'end_time':500,'definite':True}]}},final))
    class HTTP:
        async def ws_connect(self,*args,**kwargs): return ws
    ws=WS()
    monkeypatch.setattr(module,'unpack_asr',lambda data:data)
    async def run():
        cloud=VolcVoice(VoiceConfig(api_key='fixture'),HTTP())
        rows=await cloud.transcribe_source(b'\x01\x00'*8000)
        assert len(rows)==1 and rows[0].text=='陆广' and ws.closed
        import gzip,json
        body=json.loads(gzip.decompress(ws.sent[0][8:]))
        assert body['request']['enable_speaker_info'] is False
        assert 'voiceprints' not in body['request']
    asyncio.run(run())
