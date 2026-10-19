import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from time import time
from types import SimpleNamespace

import pytest

from jshi.app.voice import VoiceConversation
from jshi.core.envelope import SceneUtterance, SpeakerEvidence
from jshi.models import ModelResponse
from jshi.voice.config import VoiceConfig
from jshi.voice.direct_reply import DirectReplyState
from jshi.voice.jev import VoiceJEV
from jshi.voice.volc import Transcript
from tests.test_voice import process, FakeCloud


class Gate:
    name = 'test'
    def __init__(self): self.batches = []
    def generate(self, request):
        rows = json.loads(request.input_text)['input']['lines']
        self.batches.append(rows)
        return ModelResponse(model='test', text=json.dumps({
            'items':[{'i':r['i'], 'person':'unknown', 'certainty':'unknown',
                      'why':'本地测试没有实名证据'} for r in rows],
            'level':3,'recall_memory':False,'main_image':'omit','route':'main','reply':''}))


def test_defaults_are_twelve_seconds_or_twenty_characters(monkeypatch):
    for key in ('JSHI_VOICE_INPUT_MAX_BATCH_MS','JSHI_VOICE_INPUT_BATCH_CHARS'):
        monkeypatch.delenv(key,raising=False)
    config=VoiceConfig.from_env(asr='local',tts='local')
    assert config.input_max_batch_s==12 and config.input_batch_chars==20
    monkeypatch.setenv('JSHI_VOICE_INPUT_BATCH_CHARS','0')
    assert VoiceConfig.from_env(asr='local',tts='local').input_batch_chars==0
    with pytest.raises(ValueError):VoiceConfig('',input_batch_chars=-1)


def test_empty_input_does_not_start_or_extend_timer(process):
    async def run():
        async def send(m):pass
        gate=Gate()
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(gate),input_pause_s=.02)
        try:
            for text in ('','  ','…？！'):
                await c.accept(Transcript(text,'A',0,100,True))
            await asyncio.sleep(.06)
            assert not gate.batches and not c.input_records
            assert c.last_input_at==0 and c.input_idle.is_set()
        finally:await c.close()
    asyncio.run(run())


def test_continuous_fragments_dispatch_at_twenty_without_truncation(process):
    async def run():
        async def send(m):pass
        gate=Gate()
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(gate),input_pause_s=.5)
        try:
            await c.accept(Transcript('一二三四五六七八九十','A',0,100,True))
            await asyncio.sleep(.02)
            assert not gate.batches
            await c.accept(Transcript('甲乙丙丁戊己庚辛壬癸更多内容','A',100,200,True))
            await asyncio.wait_for(c.queue.join(),.5)
            assert len(gate.batches)==1 and len(gate.batches[0])==2
            assert gate.batches[0][1]['text'].endswith('更多内容')
            assert all(row['collection_trigger']=='chars' for row in c.input_records.values())
            assert c.last_jev is not None
        finally:await c.close()
    asyncio.run(run())


def test_pause_and_backlogged_boundary_reset_collection(process):
    async def run():
        async def send(m):pass
        gate=Gate()
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(gate),
                            input_pause_s=.03,input_batch_chars=0)
        # Queue both before the worker runs, simulating an earlier slow call.
        try:
            await c.accept(Transcript('前段','A',0,100,True))
            c.speech_input_at[('track','A')]-=.1
            await c.accept(Transcript('后段','A',2000,2100,True))
            await asyncio.wait_for(c.queue.join(),1)
            await asyncio.wait_for(c.turn_queue.join(),2)
            assert gate.batches and all(len(rows)==1 for rows in gate.batches)
            assert c.input_carry is None and c.input_idle.is_set()
            assert len(c.scene)==2
        finally:await c.close()
    asyncio.run(run())


def test_time_limit_is_started_by_text_not_idle_and_not_extended_by_partials(process):
    async def run():
        async def send(m):pass
        gate=Gate()
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(gate),
                            input_pause_s=.5,input_max_batch_s=.08,input_batch_chars=0)
        try:
            await asyncio.sleep(.12)
            assert not gate.batches
            await c.accept(Transcript('开头内容','A',0,100,True))
            for _ in range(12):
                await c.accept(Transcript('仍在说话','A',100,200,False))
                await asyncio.sleep(.01)
            await asyncio.wait_for(c.queue.join(),1)
            assert len(gate.batches)==1
            assert next(iter(c.input_records.values()))['collection_trigger']=='time'
        finally:await c.close()
    asyncio.run(run())


def test_backlog_within_same_original_window_is_not_called_fragment_by_fragment(process):
    async def run():
        async def send(m):pass
        gate=Gate()
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(gate),
                            input_pause_s=.5,input_max_batch_s=.1,input_batch_chars=20)
        try:
            await c.accept(Transcript('前半内容','A',0,100,True))
            await c.accept(Transcript('后半内容','A',100,200,True))
            for row in c.input_records.values():row['at']-=1
            await asyncio.wait_for(c.queue.join(),1)
            assert len(gate.batches)==1 and len(gate.batches[0])==2
        finally:await c.close()
    asyncio.run(run())


def test_single_request_reuses_context_and_compacts_ids_without_mutating_source():
    requests=[]
    class Model:
        def generate(self,request):
            requests.append(request)
            return ModelResponse(model='test',text='{"action":"respond","reason":"承接此前提问"}')
    speaker={'object_id':'very-long-internal-person-id','label':'lux','track_id':'long-track-id'}
    delivery={'state':'playing','now':1000000,'scene':[{'text':'此前询问','at':999000}],
              'jev_scene':'与lux交往','main_scene':[{'text':'准备说的话','at':999500}],
              'person_portraits':[{'p':'P1','portrait':'已有文字肖像','candidate_only':True,'cached_at':999}],
              'collection':{'trigger':'chars','expression_complete':False,'chars':20},
              'envelope':{'input_id':'not-for-model','parts':[]}}
    assert VoiceJEV(Model()).decide('stone','我补充一下',speaker,delivery).action=='respond'
    payload=json.loads(requests[0].input_text)
    assert payload['speaker']['track_id']=='T1' and 'object_id' not in payload['speaker']
    assert payload['delivery']['scene'][0]['at']==-1000
    assert payload['delivery']['person_portraits'][0]['cached_at']==-1000
    assert 'envelope' not in payload['delivery'] and 'time_basis' in payload
    assert 'jev_scene' not in payload['delivery']
    assert payload['delivery']['collection']['expression_complete'] is False
    assert speaker['object_id']=='very-long-internal-person-id' and delivery['scene'][0]['at']==999000
    assert '历史文字肖像' in requests[0].system_extra
    before=len(requests)
    assert VoiceJEV(Model()).decide('stone','停止',speaker,delivery).action=='respond'
    assert len(requests)==before+1


def test_cached_portrait_is_scoped_and_single_context_excludes_future_scene(process):
    result=process.experience('stone','你好',object_ref='lux',objects={'lux':'lux-id'})
    fragment=SimpleNamespace(source='person_portrait',object_id='lux-id',content='此人喜欢完整表达，请留意当前语境。')
    state=DirectReplyState()
    state.prime('stone',replace(result,current_state=replace(result.current_state,
        fragments=(*result.current_state.fragments,fragment))))
    assert state.portraits('stone',['lux-id'])['lux-id']['portrait']==fragment.content
    assert not state.portraits('other',['lux-id'])
    assert not state.portraits('stone',['lux-id'],cutoff_ms=0)
    async def run():
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            c.direct_replies=state
            from jshi.style.zone import ZoneBlock
            process.zone_store.apply_edit('stone',(),append_blocks_with_time=(
                ZoneBlock('已经发生的现场',datetime.now(timezone.utc)-timedelta(seconds=2)),
                ZoneBlock('未来写入的片场',datetime.now(timezone.utc)+timedelta(seconds=20))))
            u=SceneUtterance('原话',SpeakerEvidence('A','lux-id','lux','recognized','voiceprint_match'),0,100,
                input_id='test-turn',received_at_ms=time()*1000)
            c.input_records[u.input_id]={'collection_trigger':'chars'}
            context=c._single_jev_context({'state':'playing','jev_scene':'不送给单条的JEV片场'},[{'text':'近期接续'}],[u],time()*1000)
            assert context['scene']==[{'text':'近期接续'}]
            assert [r['text'] for r in context['main_scene']]==['已经发生的现场']
            assert context['person_portraits'][0]['portrait']==fragment.content
            assert context['person_portraits'][0]['candidate_only']
            assert 'jev_scene' not in context
            assert context['collection']['trigger']=='chars'
            assert context['collection']['expression_complete'] is False
            # Exercise the real host fallback, not only its preparation helper.
            requests=[]
            class InputModel:
                name='test'
                def generate(self,request):
                    requests.append(request)
                    return ModelResponse(model='test',text='{"action":"ignore","reason":"本地测试只核对材料"}')
            class Legacy:
                def __init__(self):
                    self.model=InputModel()
                    self.inner=VoiceJEV(self.model)
                def decide(self,*args,**kwargs):return self.inner.decide(*args,**kwargs)
            c.jev=Legacy()
            await c._judge([u],u.speaker,False)
            assert len(requests)==1
            data=json.loads(requests[0].input_text)['delivery']
            assert data['main_scene'][0]['text']=='已经发生的现场'
            assert data['person_portraits'][0]['portrait']==fragment.content
            assert 'jev_scene' not in data
            assert data['collection']['expression_complete'] is False
        finally:await c.close()
    asyncio.run(run())


def test_single_portrait_prefers_existing_l3_without_fetching_or_cutting_l1(process):
    result=process.experience('stone','你好',object_ref='lux',objects={'lux':'lux-id'})
    fragment=SimpleNamespace(source='person_portrait',object_id='lux-id',content='较长的L1人物描述。'*40,
        portrait_levels=(('L1','完整长档'),('L2','中档'),('L3','此人正在讨论项目，喜欢把问题讲完整。')),
        alternatives=('其他短描述',))
    state=DirectReplyState()
    state.prime('stone',replace(result,current_state=replace(result.current_state,
        fragments=(*result.current_state.fragments,fragment))))
    assert state.portraits('stone',['lux-id'])['lux-id']['portrait']==dict(fragment.portrait_levels)['L3']
