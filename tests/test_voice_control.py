from __future__ import annotations

import asyncio
import json
from threading import Event
from time import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jshi.app.voice import VoiceConversation, create_app
from jshi.models import ModelRequest, ModelResponse, ResponseItem, ResponsePlan, build_user
from jshi.core import SubjectState
from jshi.voice.config import VoiceConfig
from jshi.voice.dialogue import VoiceDialogue
from jshi.voice.direct_reply import reply_state
from jshi.voice.jev import VoiceJEV
from jshi.voice.volc import Transcript
from tests.test_voice import FakeCloud, Model, process


class ScriptedControl:
    """The fixture returns decisions, not a second implementation of intent matching."""
    def __init__(self, *decisions):
        self.decisions = iter(decisions)
        self.requests = []

    def generate(self, request):
        if request.purpose == 'voice_jev_scene':
            return ModelResponse(model='scene-fixture',text=json.dumps({'scene':'近期在交往。'}))
        self.requests.append(request)
        payload = json.loads(request.input_text)
        decision = next(self.decisions)
        if isinstance(decision, str):
            decision = {'control':decision}
        result = {'items':[{'i':r['i'], 'person':r.get('p') if r.get('source') == 'web_text' else 'unknown',
                           'certainty':'confirmed' if r.get('source') == 'web_text' else 'unknown',
                           'why':'本次测试提供的语义判断'} for r in payload['input']['lines']],
                  'level':3,'recall_memory':False,'main_image':'omit','control':'redirect',
                  'route':'main','reply':''}
        result.update(decision)
        return ModelResponse(text=json.dumps(result,ensure_ascii=False),model='control-fixture')


def test_dialogue_defaults_to_confirmed_segments_and_retains_raw_output(tmp_path):
    state = VoiceDialogue(tmp_path/'dialogue.json')
    assert not state.include_unplayed
    state.input('s','question','杭州天气怎么样？','lux')
    state.plan('s','answer','session',['我先问一个问题。','你要哪个城市的天气？'])
    state.bind('answer','reply')
    assert '你要哪个城市' not in state.note('s')
    state.played('reply',0)
    note = state.note('s')
    assert '我先问一个问题。' in note and '你要哪个城市' not in note
    state.stop('session')
    request = ModelRequest(purpose='subject_activity',input_text='我引用你要哪个城市的天气？',
        subject_state=SubjectState('s','匠石','测试'),
        persona_user_text='旧计划：你要哪个城市的天气？\n当前输入：我引用你要哪个城市的天气？',
        context=({'pending_text':'你要哪个城市的天气？'},))
    filtered = state.request(request)
    assert '旧计划：你要哪个城市' not in filtered.persona_user_text
    assert request.input_text in filtered.persona_user_text
    assert 'pending_text' not in filtered.context[0]
    assert '你要哪个城市' in state.path.read_text(encoding='utf-8')  # Diagnostic source remains intact.
    state.configure(True)
    assert '你要哪个城市' in state.note('s') and '未确认播出' in state.note('s')
    assert VoiceDialogue(state.path).include_unplayed


def test_dialogue_cutoff_and_subject_scope_do_not_mix_delivery(tmp_path):
    state=VoiceDialogue(tmp_path/'dialogue.json')
    before=time()*1000
    state.plan('one','answer','s',['另一主体尚未播出的回答。'])
    state.bind('answer','reply')
    state.played('reply',0)
    assert '另一主体' not in state.note('one',cutoff=before)
    state.plan('two','secret','t',['只属于第二主体的未播出正文。'])
    assert state.filter_text('只属于第二主体的未播出正文。',subject='one')=='只属于第二主体的未播出正文。'
    assert '只属于第二主体' not in state.filter_text('只属于第二主体的未播出正文。',subject='two')


def test_context_filter_preserves_current_input_and_structured_json(tmp_path):
    state=VoiceDialogue(tmp_path/'dialogue.json')
    state.plan('s','p','session',['原先未播出的回答。'])
    text='原先未播出的回答。'
    request=ModelRequest(purpose='subject_activity',subject_state=SubjectState('s','匠石','测试'),
        input_text='1. P1：'+text+'\n入口说明',input_items=({'text':text},),
        persona_user_text='【此时的输入】\n1. lux：'+text)
    assert text in state.request(request).persona_user_text
    body=json.dumps({'input':'请解释“我准备说”的含义','plan':text},ensure_ascii=False)
    filtered=json.loads(state.filter_text(body))
    assert filtered['input']=='请解释“我准备说”的含义'
    assert text not in filtered['plan']


def test_stop_reaches_player_even_if_dialogue_disk_write_fails(process,monkeypatch):
    async def run():
        sent=[]
        async def send(message):sent.append(message)
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            await c._start_plan((ResponseItem('verbal','这是尚未完成的回复。',('lux-id',)),),
                                'lux-id',c.epoch,(c.epoch,0))
            def fail():raise OSError('disk unavailable')
            with monkeypatch.context() as patch:
                patch.setattr(c.dialogue,'_save',fail)
                await c.stop('semantic_stop')
            assert c.delivery.snapshot()['state']=='stopped'
            assert any(m['type']=='stop' for m in sent)
        finally:await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('utterance,control', [
    ('哦你别说你别说了哎呀你说得太多了停一下','stop'),
    ('先让我想想，现在不用你继续解释了','stop'),
    ('别停，接着说','keep'),
    ('他说了一句停一下，我在复述','keep'),
    ('不是这个问题，现在帮我看另一件事','redirect'),
])
def test_semantic_control_uses_one_model_call_without_keyword_override(utterance,control):
    model = ScriptedControl(control)
    result = VoiceJEV(model).decide_batch('s',[{'n':1,'text':utterance}],[],{'state':'playing'})
    assert result.control == control and result.judged
    assert len(model.requests) == 1
    assert result.items[0].speaker_pick == 'unknown'  # Unknown identity does not prevent stopping.


def test_context_setting_is_same_origin_boolean_and_persistent(process):
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig('')))) as client:
            assert (await (await client.get('/api/voice-context')).json()) == {'include_unplayed':False}
            assert (await client.post('/api/voice-context',json={'include_unplayed':True})).status == 403
            headers={'Origin':str(client.make_url('')).rstrip('/')}
            assert (await client.post('/api/voice-context',headers=headers,json={'include_unplayed':'true'})).status == 400
            response=await client.post('/api/voice-context',headers=headers,json={'include_unplayed':True})
            assert await response.json() == {'include_unplayed':True}
            assert VoiceDialogue(process.repository.path.parent/'voice_dialogue.json').include_unplayed
    asyncio.run(run())


def test_standalone_text_jev_question_is_shared_with_main(process):
    first=process.experience('stone','你好',object_ref='lux',objects={'lux':'lux-id'})
    reply_state(process).prime('stone',first)
    model=ScriptedControl({'route':'self_reply','reply':'你要哪个城市的天气？'},'redirect','keep')
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig(''),jev=VoiceJEV(model)))) as client:
            headers={'Origin':str(client.make_url('')).rstrip('/')}
            response=await client.post('/text-input',headers=headers,json={'object_id':'lux-id','text':'天气怎么样？'})
            assert (await response.json())['route']=='self_reply'
            response=await client.post('/text-input',headers=headers,json={'object_id':'lux-id','text':'杭州'})
            assert response.status==200
            assert (await response.json())['route']=='main'
            assert '你要哪个城市的天气？' in build_user(process.cognition.requests[-1])
            before=len(process.cognition.requests)
            response=await client.post('/text-input',headers=headers,json={'object_id':'lux-id','text':'我在跟旁边的人说话'})
            body=await response.json()
            assert body['route']=='main'
            assert len(process.cognition.requests)==before+1
            assert '对匠石说' in build_user(process.cognition.requests[-1])
    asyncio.run(run())


@pytest.mark.parametrize('include',[False,True])
def test_jev2_filters_unplayed_source_and_main_scene_with_same_switch(process,include):
    from jshi.voice.jev_scene import JEVSceneWriter
    class Writer:
        requests=[]
        def generate(self,request):
            self.requests.append(request)
            return ModelResponse(model='scene-fixture',text=json.dumps({'scene':'当前正在交流，播放器尚未确认完整交付。'}))
    async def run():
        async def send(message):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send,scene_idle_s=60)
        writer=Writer()
        c.person_reviewer=JEVSceneWriter(writer)
        try:
            c.dialogue.configure(include)
            c.dialogue.plan('stone','pending','test',['尚未播出的具体回答。'])
            c.jev_scene_store.append('event',{'p':'匠石','text':'尚未播出的具体回答。','at':time()*1000,'basis':'尚未播出'})
            process.zone_store.apply_edit('stone',(),append_blocks=('我准备说：“尚未播出的具体回答。”',))
            c.schedule_person_review((),())
            await asyncio.wait_for(c.person_review_queue.join(),3)
            assert len(writer.requests)==1
            assert ('尚未播出的具体回答。' in writer.requests[0].input_text)==include
            snapshot=c.jev_scene_store.snapshot()
            assert snapshot['revisions'][-1]['events'][0]['payload']['text']=='尚未播出的具体回答。'
            assert snapshot['revisions'][-1]['material_window']['model_input_ids']==['event']
        finally:await c.close()
    asyncio.run(run())


def test_jev_weather_question_reaches_following_main_context_after_playback(process):
    first=process.experience('stone','你好',object_ref='lux',objects={'lux':'lux-id'})
    reply_state(process).prime('stone',first)
    model=ScriptedControl({'route':'self_reply','reply':'你要哪个城市的天气？'},'redirect')
    async def run():
        sent=[]
        async def send(message):sent.append(message)
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(model),scene_idle_s=60)
        c.person_reviewer=None
        try:
            before=len(process.cognition.requests)
            await c.typed('天气怎么样？','lux-id')
            await asyncio.wait_for(c.turn_queue.join(),3)
            assert len(process.cognition.requests)==before
            reply=next(m for m in sent if m['type']=='reply')
            assert '你要哪个城市' not in c.dialogue.note('stone')
            if c.tts_task:await c.tts_task
            await c.acknowledge({'reply_id':reply['reply_id'],'segment':0,'phase':'started'})
            await c.acknowledge({'reply_id':reply['reply_id'],'segment':0,'phase':'completed'})
            await c.typed('杭州','lux-id')
            await asyncio.wait_for(c.turn_queue.join(),3)
            user=build_user(process.cognition.requests[-1])
            assert '你要哪个城市的天气？' in user and '天气怎么样' in user and '杭州' in user
            payload=json.loads(model.requests[-1].input_text)
            assert any(r['text']=='你要哪个城市的天气？' for r in payload['conversation'])
            assert len(model.requests)==2
            assert any(r.event_type=='voice_delivery' for r in process.repository.list_history('stone',limit=100))
        finally:await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('control',['stop','redirect','keep'])
def test_control_while_thinking_does_not_wait_for_old_model_or_revive_it(process,control):
    entered,release=Event(),Event()
    class BlockingMain(Model):
        def generate(self,request):
            self.requests.append(request)
            if len(self.requests)==1:
                entered.set()
                assert release.wait(8)
                text='这是一份不该复活的旧回答。'
            else:text='这是新问题的回答。'
            return ModelResponse(text=text,model='main',response_plan=ResponsePlan('respond',items=(ResponseItem('verbal',text,('lux-id',)),)))
    process.cognition=BlockingMain()
    model=ScriptedControl('redirect',control)
    async def run():
        sent=[]
        async def send(message):sent.append(message)
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(model),scene_idle_s=60)
        c.person_reviewer=None
        try:
            await c.typed('先回答第一个问题','lux-id')
            assert await asyncio.to_thread(entered.wait,2)
            stops_before=sum(m['type']=='stop' for m in sent)
            await asyncio.wait_for(c.typed('我还有新的控制要求','lux-id'),2)
            if control!='keep':
                await asyncio.wait_for(c.turn_queue.join(),2)
                assert not release.is_set()
                assert any(m['type']=='stop' for m in sent)
                assert not any('旧回答' in m.get('text','') for m in sent if m['type']=='reply')
                if control=='redirect':assert any('新问题' in m.get('text','') for m in sent if m['type']=='reply')
                assert '旧回答' not in c.dialogue.note('stone')
            else:
                assert sum(m['type']=='stop' for m in sent)==stops_before
            release.set()
            await asyncio.wait_for(c.turn_queue.join(),3)
            await asyncio.sleep(.05)
            if control!='keep':
                assert not any('旧回答' in m.get('text','') for m in sent if m['type']=='reply')
                assert '旧回答' not in process.pending_scene.render('stone')
        finally:
            release.set()
            await c.close()
    asyncio.run(run())
