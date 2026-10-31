import asyncio
from types import SimpleNamespace
from threading import Event

from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.contracts import Transcript
from jshi.voice.online_turn import OnlineTurnGate
from jshi.voice.turns import TurnDecision
from tests.test_voice import process


def test_short_unknown_same_track_cannot_overtake_or_inherit_named_fragment():
    async def run():
        from jshi.voice.online_completion import OnlineCompletionPolicy
        audio=SessionAudio();audio.append(b'\0\0'*48000)
        delivered=[]
        policy=OnlineCompletionPolicy(SimpleNamespace(
            analyze_audio=lambda _:TurnDecision('complete'),decide=lambda rows,d:d))
        async def accept(row,**kw):delivered.append(row)
        gate=OnlineTurnGate(audio,policy,SimpleNamespace(speech_active=lambda:True),accept,max_hold_s=10)
        first=Transcript('这个时尚的','0',0,1982,True,voiceprint_id='P1')
        second=Transcript('服装','0',1982,3012,True)
        try:
            await gate.submit(first)
            assert not delivered
            await gate.submit(second)
            assert delivered == [first,second]
            assert delivered[1].voiceprint_id == ''
            await gate.flush()
            assert delivered == [first,second]
        finally:await gate.close()
    asyncio.run(run())


def test_incomplete_continuation_preserves_original_fragments():
    async def run():
        audio = SessionAudio(); audio.append(b'\0\0'*32000)
        delivered, diagnostics, sizes = [], [], []
        class Policy:
            def analyze_audio(self, samples):
                sizes.append(len(samples))
                return TurnDecision('incomplete' if len(sizes)==1 else 'complete')
            def decide(self, rows, result): return result
        async def accept(row, **kw): delivered.append((row, kw))
        gate = OnlineTurnGate(audio, Policy(), SimpleNamespace(speech_active=lambda:False), accept,
            report=lambda k,v:diagnostics.append((k,v)), max_hold_s=10)
        first=Transcript('先把', '0', 0, 1000, True, voiceprint_id='qf')
        second=Transcript('这个算出来', '0', 1000, 2000, True, voiceprint_id='qf')
        try:
            await gate.submit(first, asr_received_at_ms=111)
            assert not delivered
            await gate.submit(second, asr_received_at_ms=222)
            assert [r for r,k in delivered] == [first, second]
            assert [k['asr_received_at_ms'] for r,k in delivered] == [111,222]
            assert sizes == [16000,32000]
            assert diagnostics
        finally: await gate.close()
    asyncio.run(run())


def test_intervening_unknown_reply_breaks_previous_speakers_turn():
    async def run():
        audio=SessionAudio();audio.append(b'\0\0'*48000)
        sizes=[];delivered=[]
        class Policy:
            def analyze_audio(self,samples):
                sizes.append(len(samples))
                return TurnDecision('incomplete' if len(sizes)==1 else 'complete')
            def decide(self,rows,result):return result
        async def accept(row,**kw):delivered.append(row.text)
        gate=OnlineTurnGate(audio,Policy(),SimpleNamespace(speech_active=lambda:True),accept,max_hold_s=10)
        try:
            await gate.submit(Transcript('A问句','1',0,1000,True,voiceprint_id='A'))
            await gate.submit(Transcript('B应答','2',1100,1500,True))
            await gate.submit(Transcript('A新话轮','1',1800,2800,True,voiceprint_id='A'))
            assert delivered==['A问句','B应答','A新话轮']
            assert sizes==[16000,16000]
        finally:await gate.close()
    asyncio.run(run())


def test_hold_expiry_eof_urgent_and_unknown_never_lose_input():
    async def run():
        audio=SessionAudio();audio.append(b'\0\0'*32000)
        delivered=[]; urgent=[False]
        policy=SimpleNamespace(analyze_audio=lambda _:TurnDecision('incomplete'),decide=lambda rows,d:d)
        async def accept(row,**kw): delivered.append(row)
        gate=OnlineTurnGate(audio,policy,SimpleNamespace(speech_active=lambda:False),accept,
            urgent=lambda:urgent[0],max_hold_s=.01)
        try:
            first=Transcript('第一句', '0', 0, 500, True, voiceprint_id='qf')
            await gate.submit(first)
            await asyncio.sleep(.1)
            assert delivered == [first]
            second=Transcript('第二句', '0', 500, 1000, True, voiceprint_id='qf')
            await gate.submit(second); await gate.flush()
            assert delivered[-1]==second
            urgent[0]=True
            third=Transcript('停止', '0', 1000, 1500, True, voiceprint_id='qf')
            await gate.submit(third);assert delivered[-1]==third
            urgent[0]=False
            unknown=Transcript('未知', 'unidentified-1500', 1500, 2000, True)
            await gate.submit(unknown);assert delivered[-1]==unknown
        finally:await gate.close()
    asyncio.run(run())


def test_timeout_and_model_failure_release_without_asserting_complete():
    async def run():
        import time
        audio=SessionAudio();audio.append(b'\0\0'*16000)
        delivered=[]
        def slow(_):time.sleep(.1);return TurnDecision('incomplete')
        policy=SimpleNamespace(analyze_audio=slow,decide=lambda rows,d:d)
        async def accept(row,**kw):delivered.append(row)
        gate=OnlineTurnGate(audio,policy,SimpleNamespace(speech_active=lambda:False),accept,timeout_s=.01)
        row=Transcript('测试','0',0,1000,True,voiceprint_id='qf')
        try:
            await gate.submit(row);assert delivered==[row]
            await asyncio.sleep(.15);assert delivered==[row]
        finally:await gate.close()
    asyncio.run(run())


def test_close_discards_old_pending_before_hold_deadline():
    async def run():
        audio=SessionAudio();audio.append(b'\0\0'*16000)
        delivered=[];active=[True]
        policy=SimpleNamespace(analyze_audio=lambda _:TurnDecision('incomplete'),decide=lambda rows,d:d)
        async def accept(row,**kw):delivered.append(row)
        gate=OnlineTurnGate(audio,policy,SimpleNamespace(speech_active=lambda:active[0]),accept,max_hold_s=10)
        await gate.submit(Transcript('还在说','0',0,1000,True,voiceprint_id='qf'))
        await asyncio.sleep(.1)
        assert not delivered
        await gate.close();active[0]=False
        await asyncio.sleep(.1)
        assert not delivered
    asyncio.run(run())


def test_continuous_ambient_speech_cannot_hold_final_indefinitely():
    async def run():
        audio=SessionAudio();audio.append(b'\0\0'*16000)
        delivered=[];done=asyncio.Event();reports=[]
        policy=SimpleNamespace(analyze_audio=lambda _:TurnDecision('incomplete'),decide=lambda rows,d:d)
        async def accept(row,**kw):delivered.append(row);done.set()
        gate=OnlineTurnGate(audio,policy,SimpleNamespace(speech_active=lambda:True),accept,
            max_hold_s=.01,report=lambda kind,data:reports.append((kind,data)))
        row=Transcript('已定稿但还没说完','0',0,1000,True,voiceprint_id='qf')
        try:
            await gate.submit(row)
            await asyncio.wait_for(done.wait(),1)
            assert delivered==[row]
            timing=next(data for kind,data in reports if kind=='online_acoustic_turn')
            assert timing['turn_state']=='incomplete'
            assert timing['turn_release_reason']=='hold_deadline'
            await gate.flush();assert delivered==[row]
        finally:await gate.close()
    asyncio.run(run())


def test_appending_same_speaker_does_not_reset_oldest_final_deadline(monkeypatch):
    from jshi.voice.turns import PendingTurn,TurnFragment,TurnCoordinator
    import jshi.voice.turns as turns
    policy=SimpleNamespace()
    coordinator=TurnCoordinator(policy,max_hold_s=2)
    first=Transcript('前半句','0',0,1000,True,voiceprint_id='qf')
    second=Transcript('后半句','0',1000,2000,True,voiceprint_id='qf')
    coordinator.pending[('voice','qf')]=PendingTurn([
        TurnFragment(first,None,{'_asr_return_mono':100.,'turn_state':'incomplete'}),
        TurnFragment(second,None,{'_asr_return_mono':101.9,'turn_state':'incomplete'})],touched=101.9)
    monkeypatch.setattr(turns,'monotonic',lambda:102.01)
    try:
        rows=[];timings=[]
        coordinator.poll(rows,timings,allow_expiry=False,hard_max_hold=True)
        assert rows==[first,second]
        assert all(t['turn_release_reason']=='hold_deadline' and t['turn_state']=='incomplete' for t in timings)
    finally:coordinator.close()


def test_voice_websocket_feeds_and_flushes_online_acoustic_gate(process, monkeypatch):
    from aiohttp.test_utils import TestClient,TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    import jshi.voice.streaming_asr as streaming
    import jshi.voice.online_turn as module
    import jshi.voice.turns as turns
    async def run():
        ready=asyncio.Event();calls=[]
        class ASR:
            async def send_audio(self,pcm):calls.append(('sent',pcm));ready.set()
            async def results(self):
                await ready.wait()
                yield [Transcript('你好','0',0,100,True)],True
            async def close(self):pass
        async def open_stream(*args):calls.append(('open_asr',None));return ASR()
        class Gate:
            def feed(self,pcm):calls.append(('feed',pcm))
            def speech_active(self):return False
            async def submit(self,row,**kwargs):calls.append(('submit',row.text))
            async def flush(self):calls.append(('flush',None))
            async def close(self):calls.append(('close',None))
        monkeypatch.setattr(streaming,'open_stream',open_stream)
        monkeypatch.setattr(turns,'build_turn_policy',lambda *args:object())
        def build_gate(*args,**kwargs):
            calls.append(('initialize_local_gate',None))
            return Gate()
        monkeypatch.setattr(module,'build_online_turn_gate',build_gate)
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig('test')))) as client:
            ws=await client.ws_connect('/voice',headers={'Origin':str(client.make_url('/')).rstrip('/')})
            while True:
                message=await ws.receive_json()
                if message.get('type')=='ready':break
            await ws.send_bytes(b'\0\0'*1600)
            async for _ in ws:pass
        assert ('feed',b'\0\0'*1600) in calls
        assert calls.index(('initialize_local_gate',None)) < calls.index(('open_asr',None))
        assert ('submit','你好') in calls
        assert ('flush',None) in calls and ('close',None) in calls
    asyncio.run(run())


def test_vad_window_is_observable_without_committing_text():
    async def run():
        audio=SessionAudio();reports=[];delivered=[]
        detector=SimpleNamespace(feed=lambda _: [(range(1600),200)],total=6400,speech_active=lambda:False)
        async def accept(row,**kw):delivered.append(row)
        gate=OnlineTurnGate(audio,SimpleNamespace(),detector,accept,
            report=lambda kind,data:reports.append((kind,data)))
        try:
            gate.feed(b'\0\0'*1600)
            assert not delivered
            assert reports[0][0]=='online_acoustic_window'
            assert reports[0][1]['start_ms']==200 and reports[0][1]['end_ms']==300
            assert reports[0][1]['semantic_complete'] is None
        finally:await gate.close()
    asyncio.run(run())
