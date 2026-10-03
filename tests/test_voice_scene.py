from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jshi.core.envelope import SceneUtterance, SpeakerEvidence
from jshi.models import ModelResponse, ResponseItem, ResponsePlan
from jshi.skill.cognition import _to_model_response
from jshi.voice.diarization import LocalDiarizer
from jshi.voice.jev import explicit_name
from jshi.voice.volc import Transcript
from jshi.app.voice import VoiceConversation
from tests.test_voice import process, FakeCloud, Model


def test_unassigned_bucket_does_not_create_visitors_or_claim_their_history(process):
    from jshi.voice.identity import VoiceIdentities
    ids=VoiceIdentities(process.profiles,'stone','session')
    a=ids.resolve('0',cluster_id='A')
    unclear=ids.resolve('0',uncertain=True)
    assert unclear.object_id!=a.object_id and unclear.label=='声音归属待定'
    assert ids.resolve('8',uncertain=True).object_id==unclear.object_id
    named,note=ids.introduce('0','lux',uncertain=True)
    assert named.object_id=='lux-id' and '本句' in note
    assert ids.associated(unclear).object_id==unclear.object_id
    assert ids.resolve('0',cluster_id='A').object_id==a.object_id
    assert len(ids.clusters)==1


def test_unknown_greeting_can_enter_main_flow(process):
    from jshi.voice.jev import VoiceJEV
    class Gate:
        name='test'
        def generate(self,request):
            assert '都不是忽略或拒绝进入主流程的理由' in request.system_extra
            return ModelResponse(text='{"action":"respond","reason":"向匠石打招呼"}',model='test')
    async def run():
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(Gate()),input_pause_s=.02)
        try:
            await c.accept(Transcript('匠石你好','0',0,2000,True,identity_uncertain=True))
            await asyncio.wait_for(c.queue.join(),1)
            await asyncio.wait_for(c.turn_queue.join(),2)
            assert len(process.cognition.requests)==1
            assert c.scene[-1].speaker.label=='声音归属待定'
        finally:await c.close()
    asyncio.run(run())


def test_anonymous_voice_survives_cloud_track_changes_then_gets_a_name(process):
    from jshi.voice.identity import VoiceIdentities
    ids = VoiceIdentities(process.profiles, 'stone', 'session')
    first = ids.resolve('0', cluster_id='local-A')
    second = ids.resolve('7', cluster_id='local-A')
    assert first.object_id == second.object_id
    assert first.label.startswith('未命名访客')
    other = ids.resolve('7', cluster_id='local-B')
    assert other.object_id != first.object_id
    named, _ = ids.introduce('2', 'lux', cluster_id='local-A')
    assert ids.resolve('8', cluster_id='local-A').object_id == 'lux-id'
    assert ids.associated(first).object_id == 'lux-id'
    assert ids.associated(other).object_id != named.object_id


def test_unfinished_sentence_waits_for_continuation(process):
    async def run():
        async def send(m): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send, input_pause_s=.04)
        try:
            await c.accept(Transcript('还有一种问题是', 'A', 0, 2000, True))
            await asyncio.sleep(.2)
            assert not process.cognition.requests
            await c.accept(Transcript('怎么把声纹接上名字', 'A', 2500, 4000, True))
            await asyncio.wait_for(c.queue.join(), 1)
            await asyncio.wait_for(c.turn_queue.join(), 2)
            assert len(process.cognition.requests) == 1
            assert '还有一种问题是' in process.cognition.requests[0].input_text
            assert '怎么把声纹接上名字' in process.cognition.requests[0].input_text
        finally: await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('state', ['completed', 'playing'])
def test_jev_background_gate_records_scene_without_main_call(process, state):
    from jshi.voice.jev import VoiceJEV
    class Gate:
        name='test'
        def __init__(self): self.requests=[]
        def generate(self, request):
            self.requests.append(request)
            return ModelResponse(text='{"action":"ignore","reason":"对旁人说话"}',model='test')
    async def run():
        gate=Gate()
        async def send(m): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(gate),input_pause_s=.02)
        if state=='playing':
            reply=c.delivery.begin('正在说的内容。','lux-id')
            c.delivery.acknowledge(reply.reply_id,0,'started')
        try:
            await c.accept(Transcript('你作业写完没有', 'other', 0, 2000, True))
            await asyncio.wait_for(c.queue.join(),1)
            assert len(gate.requests)==1
            assert len(c.scene)==1
            assert not process.cognition.requests and c.turn_queue.empty()
            if state=='playing':assert c.delivery.snapshot()['state']=='playing'
        finally:await c.close()
    asyncio.run(run())


def test_spelled_name_with_greeting_links_existing_lux(process):
    from jshi.voice.identity import VoiceIdentities
    ids=VoiceIdentities(process.profiles,'stone','session')
    assert explicit_name('我是 L U X 你好')=='LUX'
    who,note=ids.introduce('voice-A',explicit_name('我是 L U X 你好'))
    assert who.object_id=='lux-id' and who.status=='introduced'


@pytest.mark.parametrize('stage', ['cognition', 'write_zone'])
def test_busy_main_collects_multiple_batches_for_one_next_model_call(process, stage):
    from threading import Event
    from jshi.voice.jev import InterruptDecision
    entered, release = Event(), Event()
    class Gate(Model):
        def generate(self, request):
            if not self.requests:
                entered.set()
                assert release.wait(5)
            return super().generate(request)
    gate = Gate()
    setattr(process, stage, gate)
    class Judge:
        def __init__(self): self.calls = 0
        def decide(self, *args, **kwargs):
            self.calls += 1
            return InterruptDecision('respond', 'direct continuation')
    async def run():
        judge=Judge()
        events=[]
        async def send(m): events.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=judge,input_pause_s=.04)
        try:
            await c.accept(Transcript('我是 lux','A',0,2000,True))
            assert await asyncio.to_thread(entered.wait,2)
            calls=judge.calls
            await c.accept(Transcript('我想去看看','A',2500,3500,True))
            await asyncio.wait_for(c.queue.join(),1)
            await c.accept(Transcript('我是小明','B',3600,4500,True))
            await asyncio.wait_for(c.queue.join(),1)
            await c.accept(Transcript('再补充一句','A',4600,5500,True))
            await asyncio.wait_for(c.queue.join(),1)
            assert judge.calls==calls
            assert c.turn_queue.qsize()==1
            release.set()
            await asyncio.wait_for(c.turn_queue.join(),3)
            requests=process.cognition.requests
            assert len(requests)==2
            second=requests[1]
            assert all(t in second.input_text for t in ('我想去看看','我是小明','再补充一句'))
            assert 'lux' in second.input_text and '小明' in second.input_text
            assert '【本批新发言】' in second.transport_context
            assert '轨迹=A' in second.input_text and '轨迹=B' in second.input_text
            assert '上一轮实测耗时' in second.transport_context
            if stage == 'cognition':
                assert '发言与回应先后' in second.transport_context
            assert '上午去古镇' in second.transport_context
        finally:
            release.set()
            await c.close()
    asyncio.run(run())


def test_short_pause_and_partial_continuation_form_one_input(process):
    async def run():
        async def send(m): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send,input_pause_s=.12)
        try:
            await c.accept(Transcript('你好','A',0,1000,True))
            await asyncio.sleep(.08)
            await c.accept(Transcript('我想','A',1000,1500,False))
            await asyncio.sleep(.08)
            assert not process.cognition.requests
            await c.accept(Transcript('我想问一件事','A',1000,2400,True))
            await asyncio.wait_for(c.queue.join(),1)
            await asyncio.wait_for(c.turn_queue.join(),2)
            assert len(process.cognition.requests)==1
            assert '你好' in process.cognition.requests[0].input_text
            assert '我想问一件事' in process.cognition.requests[0].input_text
        finally: await c.close()
    asyncio.run(run())


def test_diagnostics_use_actual_completed_turn_prompt_and_timing(process):
    async def run():
        events=[]
        async def send(m): events.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            await c.diagnostics('prompt')
            assert '还没有' in events[-1]['text']
            await c.accept(Transcript('我是 lux 你好','A',0,2000,True))
            await asyncio.wait_for(c.queue.join(),3)
            await asyncio.wait_for(c.turn_queue.join(),3)
            for section in ('prompt','timing','tool'):
                await c.diagnostics(section)
                assert events[-1]['type']=='debug'
                assert events[-1]['activity_id']==c.last_debug['activity_id']
            prompt=next(m['text'] for m in events if m.get('activity_id') and m.get('section')=='prompt')
            assert 'system' in prompt and '我是 lux 你好' in prompt
        finally: await c.close()
    asyncio.run(run())


def test_multi_target_plan_keeps_order_and_all_verbal_text():
    plan=_to_model_response({'response_plan':{'mode':'respond','items':[
        {'channel':'verbal','text':'先回答甲。','target_ids':['A']},
        {'channel':'verbal','text':'再回答乙。','target_ids':['B']},
        {'channel':'verbal','text':'最后对大家说。','target_ids':[]},
    ]}},'test').response_plan
    assert plan.verbal_text()=='先回答甲。\n再回答乙。\n最后对大家说。'
    assert [i.target_ids for i in plan.items]==[('A',),('B',),()]


def test_background_speech_reaches_scene_even_when_jev_keeps_playing(process):
    async def run():
        events=[]
        async def send(m):events.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        current=c.delivery.begin('原来的回答。','lux-id')
        c.delivery.acknowledge(current.reply_id,0,'started')
        try:
            await c.accept(Transcript('电视正在播放故事','tv-track',1000,3000,True))
            await asyncio.wait_for(c.queue.join(),3)
            await asyncio.wait_for(c.turn_queue.join(),2)
            assert c.scene[0].speaker.track_id=='tv-track'
            assert any(m['type']=='timeline' and m['start_ms']==1000 for m in events)
            assert c.delivery.current.reply_id==current.reply_id
            assert not any(m['type']=='stop' for m in events)
            assert not c.pending_plans
            assert not process.cognition.requests
        finally:await c.close()
    asyncio.run(run())


def test_multiple_speakers_in_one_batch_use_scene_instead_of_last_person(process):
    async def run():
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            await c.accept(Transcript('我是 L U X 你好','A',0,2000,True))
            await c.accept(Transcript('电视台词','B',1000,3000,True))
            await asyncio.wait_for(c.queue.join(),3)
            await asyncio.wait_for(c.turn_queue.join(),2)
            request=process.cognition.requests[0]
            assert request.speaker.label=='语音现场'
            assert 'lux-id' in request.transport_context and '电视台词' in request.input_text
            assert len(c.scene)==2
        finally:await c.close()
    asyncio.run(run())


def test_targeted_segments_play_in_order_and_enter_actual_delivery(process):
    async def run():
        events=[]
        async def send(m):events.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            items=(ResponseItem('verbal','先说甲。',('A',)),ResponseItem('verbal','再说乙。',('B',)))
            await c._start_plan(items,'scene',0,(0,1))
            await c.tts_task
            d=c.delivery.current
            assert d.targets==(('A',),('B',))
            assert [m['segment'] for m in events if m['type']=='audio']==[0,1]
            await c.acknowledge({'reply_id':d.reply_id,'segment':0,'phase':'completed'})
            ledger=process.activity_ledger.list_experiences('stone')
            assert any(s.text_raw=='（播放器确认此句播放完成）先说甲。' and s.mentioned_object_ids==('A',) for s in ledger)
        finally:await c.close()
    asyncio.run(run())


def test_diarization_preserves_offsets_and_never_identifies_overlapping_mix():
    np=pytest.importorskip('numpy')
    spans=[SimpleNamespace(start=0.,end=2.,speaker=0),SimpleNamespace(start=1.,end=3.,speaker=1),
           SimpleNamespace(start=3.,end=5.,speaker=1)]
    class Engine:
        def process(self,a):return SimpleNamespace(sort_by_start_time=lambda:spans)
    class Speakers:
        calls=0
        def identify(self,a):self.calls+=1;return 'known-B','local:test:B',.9
    class Recognizer:
        def create_stream(self):return SimpleNamespace(accept_waveform=lambda *a:None,result=SimpleNamespace(text='声音'))
        def decode_stream(self,s):pass
    speakers=Speakers()
    out=LocalDiarizer(Engine(),speakers,Recognizer()).transcribe(np.zeros(80000,dtype='float32'),10000)
    assert [(t.start_ms,t.end_ms,t.overlap) for t in out]==[(10000,12000,True),(11000,13000,True),(13000,15000,False)]
    assert speakers.calls==1
    assert not out[0].voiceprint_id and not out[1].voiceprint_id
    assert out[2].track_id=='known-B'
