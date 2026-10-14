import asyncio
import json
import pytest
from dataclasses import replace

from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV
from jshi.voice.wait_ack import WaitAckTiming
from jshi.app.voice import VoiceConversation
from tests.test_voice import FakeCloud, envelope, process


@pytest.fixture(autouse=True)
def enable_optional_ack_for_feature_tests(monkeypatch):
    monkeypatch.setenv('JSHI_JEV_WAIT_ACK_ENABLED', 'true')


def test_waiting_ack_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv('JSHI_JEV_WAIT_ACK_ENABLED', raising=False)
    stats = WaitAckTiming(tmp_path/'wait.json')
    assert not stats.guidance('a', allowed=True)['allowed']
    assert not stats.claim_ack()


def test_latency_guidance_is_measured_separate_and_persisted(tmp_path):
    stats = WaitAckTiming(tmp_path/'wait.json')
    assert stats.guidance('a', allowed=True)['levels']['L1']['samples'] == 0
    for delay in (4, 6, 5):
        stats.observe('a', 1, delay)
    loaded = WaitAckTiming(stats.path)
    guidance = loaded.guidance('a', allowed=True)
    assert guidance['levels']['L1']['expected_seconds'] == 5
    assert guidance['levels']['L2']['samples'] == 0
    assert loaded.guidance('b', allowed=True)['levels']['L1']['samples'] == 0
    assert guidance['levels']['L1']['max_chars'] <= 16
    assert not loaded.guidance('a', allowed=False)['allowed']


def test_waiting_reply_requires_main_permission_and_budget(tmp_path):
    class Model:
        def generate(self, request):
            return ModelResponse(model='mock', text=json.dumps({'items':[
                {'i':1,'person':'P1','certainty':'confirmed','why':'已选人物'}],
                'level':1,'recall_memory':False,'route':'main','reply':'',
                'waiting_reply':'嗯，我想一下。'}))
    gate = VoiceJEV(Model())
    batch = [{'n':1,'who':'P1','text':'匠石，帮我分析一下','source':'web_text'}]
    stats = WaitAckTiming(tmp_path/'wait.json')
    assert gate.decide_batch('s',batch,[],{'waiting':stats.guidance('a',allowed=True)}).waiting_reply
    assert not gate.decide_batch('s',batch,[],{'waiting':stats.guidance('a',allowed=False)}).waiting_reply
    stats.observe('a',1,1)
    assert not gate.decide_batch('s',batch,[],{'waiting':stats.guidance('a',allowed=True)}).waiting_reply


@pytest.mark.parametrize('finish', ['completed', 'stopped'])
def test_formal_reply_waits_for_ack_playback_but_cognition_runs(process, finish):
    async def run():
        sent=[]
        async def send(message):sent.append(message)
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            e,_=envelope(process,'帮我分析一下')
            e=replace(e,jev_calls=({'waiting_reply':'嗯，我想一下。','input_ids':[],
                                   'main_prompt_hint':{'level':1},'ms':0},))
            await c._respond(e,c.epoch)
            await asyncio.sleep(0)
            assert process.cognition.requests  # No playback completion yet.
            ack=c.delivery.current
            assert ack.texts==('嗯，我想一下。',)
            if c.tts_task:await c.tts_task
            assert c.pending_plans
            assert len([m for m in sent if m['type']=='reply'])==1
            if finish == 'stopped':
                await c.stop('用户要求停止')
                assert not c.pending_plans
                assert len([m for m in sent if m['type']=='reply'])==1
                return
            await c.acknowledge({'reply_id':ack.reply_id,'segment':0,'phase':'started'})
            await c.acknowledge({'reply_id':ack.reply_id,'segment':0,'phase':'completed'})
            assert len([m for m in sent if m['type']=='reply'])==2
            assert c.delivery.current.reply_id!=ack.reply_id
            assert c.jev_scene_store.snapshot()['events'][-1]['payload']['basis']=='等待接话已播出'
        finally:
            await c.close()
    asyncio.run(run())


def test_waiting_ack_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv('JSHI_JEV_WAIT_ACK_ENABLED', 'false')
    assert not WaitAckTiming(tmp_path/'wait.json').guidance('a', allowed=True)['allowed']


@pytest.mark.parametrize('level', [1, 2, 3])
def test_five_choices_map_to_the_selected_level(tmp_path, level):
    class Gate:
        def generate(self, request):
            schema = json.loads(request.system_extra.split('JSON Schema：')[1])
            assert schema['properties']['route']['enum'] == ['main']
            return ModelResponse(model='mock', text=json.dumps({'items':[
                {'i':1,'person':'P1','certainty':'confirmed','why':'用户已选人物'}],
                'level':level,'recall_memory':False,'main_image':'omit','route':'main',
                'reply':'','waiting_reply':'我想一下。'}))
    waiting=WaitAckTiming(tmp_path/'stats.json').guidance('m',allowed=True)
    result=VoiceJEV(Gate()).decide_batch('s',[{'n':1,'who':'P1','text':'请帮我分析','source':'web_text'}],[],{'waiting':waiting})
    assert result.front_choice == f'main_l{level}_ack'


def test_waiting_ack_cooldown_does_not_disable_main(tmp_path, monkeypatch):
    monkeypatch.setenv('JSHI_JEV_WAIT_ACK_ENABLED','true')
    clock=[100.0]
    monkeypatch.setattr('jshi.voice.wait_ack.monotonic',lambda:clock[0])
    stats=WaitAckTiming(tmp_path/'stats.json')
    assert stats.claim_ack()
    assert not stats.guidance('m',allowed=True)['allowed']
    assert not stats.claim_ack()
    clock[0]+=30
    assert stats.guidance('m',allowed=True)['allowed']
