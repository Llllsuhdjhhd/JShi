import asyncio
import json
from time import monotonic
from types import SimpleNamespace

import pytest

from jshi.decision.base import DecisionRequest, DecisionResult, validate_answers
from jshi.decision.policies import COLLISION, CONTINUATION, collision_action, continuation_action
from jshi.decision.service import DecisionService
from jshi.decision.system_one import SystemOnePort
from jshi.core.envelope import SceneUtterance, SpeakerEvidence
from jshi.voice.entry_scheduler import EntryScheduler
from tests.test_voice import process


def answers(questions, choices):
    return {key: {'type':'choice', 'choice': choices[key], 'confidence':.99,
                  'probabilities':{option:float(option==choices[key]) for option in question['criteria']}}
            for key, question in questions.items()}


@pytest.mark.parametrize('provider,model', [('bailian','decision-model-preview'), ('typesafe','jev-latest')])
def test_native_payload_and_checked_result(provider, model):
    async def scenario():
        async def transport(payload):
            assert set(payload) == {'model','state','questions'}
            assert payload['model'] == model
            return 200, {'model': model, 'answers':answers(COLLISION, {'relation':'replace','old_work':'stop'}),
                         'latency_ms':12, 'request_id':'trace', 'usage':{'input_tokens':100}}
        port = SystemOnePort(provider, model, 'https://example.test/v1/systemone', 'secret', transport=transport)
        result = await port.decide(DecisionRequest('collision', {'old':'A','new':'B'}, COLLISION), deadline=monotonic()+1)
        assert result.status == 'success' and collision_action(result) == 'merge'
        assert result.server_ms == 12 and result.usage['input_tokens'] == 100
        await port.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['missing','nan','wrong_type','wrong_option','bad_sum'])
def test_invalid_answers_fail_closed(kind):
    data = answers(COLLISION, {'relation':'background','old_work':'continue'})
    if kind == 'missing': data.pop('relation')
    elif kind == 'nan': data['relation']['confidence'] = float('nan')
    elif kind == 'wrong_type': data['relation']['type'] = 'noul'
    elif kind == 'wrong_option': data['relation']['choice'] = 'delete_all'
    else: data['relation']['probabilities']['background'] = .4
    with pytest.raises(ValueError): validate_answers(COLLISION, data)


def test_missing_confidence_is_not_fabricated_from_probability():
    data = answers(COLLISION, {'relation':'background','old_work':'continue'})
    data['relation'].pop('confidence')
    validate_answers(COLLISION, data)
    assert collision_action(DecisionResult('success', data)) == 'merge'


def test_deadline_and_no_retry_or_error_body():
    async def scenario():
        calls = []
        async def slow(payload):
            calls.append(payload)
            await asyncio.Event().wait()
        port = SystemOnePort('bailian','m','https://example.test','secret',transport=slow)
        result = await port.decide(DecisionRequest('collision',{},COLLISION), deadline=monotonic()+.01)
        assert result.status == 'timeout' and len(calls) == 1
        async def failure(payload): return 401, {'error':'secret private information'}
        port.transport = failure
        result = await port.decide(DecisionRequest('collision',{},COLLISION), deadline=monotonic()+1)
        assert result.reason == 'http_401' and 'secret' not in repr(result)
    asyncio.run(scenario())


def test_settings_switches_without_saving_key_or_modifying_chat(tmp_path, monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY','ali-secret')
    monkeypatch.setenv('TYPESAFE_API_KEY','type-secret')
    service = DecisionService(tmp_path)
    assert service.public()['providers']['bailian']['has_key']
    service.save({'provider':'typesafe','mode':'active'})
    assert 'secret' not in service.path.read_text(encoding='utf8')
    assert 'secret' not in json.dumps(service.public())
    assert DecisionService(tmp_path).config['provider'] == 'typesafe'
    with pytest.raises(ValueError):
        service.save({'providers':{'bailian':{'endpoint':'https://api.typesafe.ai/v1/systemone'}}})
    assert service.config['provider'] == 'typesafe'


def test_existing_bailian_key_reused_without_copying(tmp_path, monkeypatch):
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    monkeypatch.delenv('JSHI_API_QWEN_API_KEY', raising=False)
    (tmp_path/'model_settings.local.json').write_text(json.dumps({'services':{'qwen':{
        'api_key':'local-secret', 'endpoint':'https://dashscope.aliyuncs.com/compatible-mode/v1'}}}), encoding='utf8')
    service = DecisionService(tmp_path)
    assert service.public()['providers']['bailian']['credential_source'] == 'existing_qwen_settings'
    service.save({'mode':'shadow'})
    assert 'local-secret' not in service.path.read_text(encoding='utf8')


def speech(text, n=0):
    return SceneUtterance(text,SpeakerEvidence('track','person','甲','recognized'), n*1000,(n+1)*1000,input_id=str(n))


@pytest.mark.parametrize('action,expected', [('continue','old-result'),('merge',None),('stop',None)])
def test_inflight_entry_waits_for_collision_and_preserves_originals(tmp_path, action, expected):
    async def scenario():
        service = DecisionService(tmp_path)
        service.config['mode'] = 'active'
        decision_started, release_decision, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        old_finished = asyncio.Event()
        stopped=[]
        async def decide(purpose, state, revision):
            assert state['old'][0]['text']=='A' and state['new'][0]['text']=='B'
            assert state['old'][0]['person']==state['new'][0]['person']
            decision_started.set()
            await release_decision.wait()
            choices = {'continue':{'relation':'independent','old_work':'continue'},
                       'merge':{'relation':'continue','old_work':'hold'},
                       'stop':{'relation':'stop','old_work':'stop'}}[action]
            return DecisionResult('success', answers(COLLISION, choices))
        service.decide = decide
        async def stop(reason): stopped.append(reason)
        scheduler = EntryScheduler(service,on_stop=stop)
        async def operation():
            started.set()
            await old_finished.wait()
            assert await scheduler.current.permit()
            return 'old-result'
        task=asyncio.create_task(scheduler.run((speech('A'),),operation))
        await started.wait()
        scheduler.notify((speech('B',1),))
        await decision_started.wait()
        old_finished.set()
        await asyncio.sleep(0)
        assert not task.done()
        release_decision.set()
        assert await asyncio.wait_for(task,1) == expected
        assert bool(stopped) == (action=='stop')
        await scheduler.close()
    asyncio.run(scenario())


def test_newer_C_never_released_by_AB_decision(tmp_path):
    async def scenario():
        service=DecisionService(tmp_path);service.config['mode']='active'
        started=asyncio.Event();releases=[asyncio.Event(),asyncio.Event()];counts=[]
        async def decide(purpose,state,revision):
            n=len(counts);counts.append(len(state['new']));started.set()
            await releases[n].wait()
            return DecisionResult('success',answers(COLLISION,{'relation':'independent','old_work':'continue'}))
        service.decide=decide
        scheduler=EntryScheduler(service)
        op_started=asyncio.Event();end=asyncio.Event()
        async def operation():op_started.set();await end.wait();return 'ok'
        task=asyncio.create_task(scheduler.run((speech('A'),),operation))
        await op_started.wait();scheduler.notify((speech('B',1),));await started.wait()
        scheduler.notify((speech('C',2),));releases[0].set()
        await asyncio.sleep(0);await asyncio.sleep(0)
        assert not scheduler.current.approved.is_set()
        releases[1].set();end.set()
        assert await task=='ok' and counts==[1,2]
    asyncio.run(scenario())


@pytest.mark.parametrize('impact,expression,route',[('reuse','complete','reuse'),('revise','complete','revise'),
    ('restart','complete','restart'),('reuse','incomplete','normal'),('uncertain','complete','normal'),
    ('uncertain','uncertain','normal')])
def test_continuation_routing(impact,expression,route):
    result=DecisionResult('success',answers(CONTINUATION,{'impact':impact,'expression':expression,'address':'direct'}))
    assert continuation_action(result)==route


def test_continuation_fault_does_not_request_a_spoken_restart():
    failed = DecisionResult('timeout', answers(CONTINUATION, {
        'impact': 'restart', 'expression': 'complete', 'address': 'direct'}))
    assert continuation_action(failed) == 'normal'


def test_cooldown_stays_inside_one_active_purpose(tmp_path, monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'ali-secret')

    def service_at(path, mode):
        service = DecisionService(path)
        service.config['mode'] = mode
        service.config['providers']['bailian']['endpoint'] = 'https://workspace.maas.aliyuncs.com/v1/systemone'
        class Port:
            async def decide(self, request, *, deadline):
                return DecisionResult('timeout', reason='deadline')
            async def close(self):
                pass
        service.factory = lambda *args: Port()
        return service

    async def scenario():
        shadow = service_at(tmp_path / 'shadow', 'shadow')
        observed = await shadow.decide('entry', {'material': '观察'})
        shadow.config['mode'] = 'active'
        after = await shadow.decide('entry', {'material': '观察之后'})
        assert observed.reason == after.reason == 'deadline'
        active = service_at(tmp_path / 'active', 'active')
        continuation = await active.decide('continuation', {'input': ['前段']})
        entry = await active.decide('entry', {'material': '一句'})
        collision = await active.decide('collision', {'old': [], 'new': []})
        collision_again = await active.decide('collision', {'old': [], 'new': []})
        assert continuation.reason == entry.reason == collision.reason == 'deadline'
        assert collision_again.reason == 'cooldown'
    asyncio.run(scenario())


def test_collision_report_uses_this_result_not_the_shared_last(tmp_path):
    async def scenario():
        service = DecisionService(tmp_path)
        service.config['mode'] = 'active'
        reports = []
        async def decide(purpose, state, revision=0, **kwargs):
            service.last = {'purpose': 'entry', 'status': 'timeout', 'reason': 'other', 'model': 'stale'}
            return DecisionResult('success', answers(COLLISION, {'relation': 'independent', 'old_work': 'continue'}),
                                  model='this-call')
        service.decide = decide
        scheduler = EntryScheduler(service, report=lambda *args: reports.append(args))
        started, done = asyncio.Event(), asyncio.Event()
        async def operation():
            started.set()
            await done.wait()
            return 'ok'
        task = asyncio.create_task(scheduler.run((speech('A'),), operation))
        await started.wait()
        scheduler.notify((speech('B', 1),))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        done.set()
        assert await asyncio.wait_for(task, 1) == 'ok'
        assert reports[0][1]['model'] == 'this-call' and reports[0][1]['status'] == 'success'
        assert reports[0][1]['reason'] == ''
    asyncio.run(scenario())


def test_shadow_never_blocks_old_call(tmp_path):
    async def scenario():
        service=DecisionService(tmp_path);service.config['mode']='shadow'
        started=asyncio.Event();end=asyncio.Event()
        async def decide(*args):await asyncio.Event().wait()
        service.decide=decide;scheduler=EntryScheduler(service)
        async def operation():started.set();await end.wait();return 'ok'
        task=asyncio.create_task(scheduler.run((speech('A'),),operation))
        await started.wait();scheduler.notify((speech('B',1),));end.set()
        assert await asyncio.wait_for(task,1)=='ok'
    asyncio.run(scenario())


def test_real_voice_collision_rejudges_combined_input_once(process):
    from threading import Event
    from tests.test_voice import FakeCloud, Model as MainModel
    from tests.test_jev_split_modes import Model, entry, preparation
    from jshi.voice.jev import VoiceJEV
    from jshi.voice.contracts import Transcript
    from jshi.app.voice import VoiceConversation
    first_started,release=Event(),Event()
    def first(request):
        first_started.set()
        assert release.wait(3)
        return entry(items=[{'i':1,'person':'unknown','certainty':'unknown','why':'声音未知'}])
    second=entry(items=[{'i':i,'person':'unknown','certainty':'unknown','why':'声音未知'} for i in (1,2)],
                 process_refs=['I1','I2'],decision_refs=['I1','I2'])
    model=Model([first,second,preparation()])
    process.cognition=MainModel()
    async def scenario():
        async def send(message):pass
        voice=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(model,mode='split',fallback_models=[]))
        voice.decisions.config['mode']='active'
        async def decide(*args):return DecisionResult('success',answers(COLLISION,{'relation':'replace','old_work':'stop'}))
        voice.decisions.decide=decide
        try:
            await voice.accept(Transcript('匠石，查明天去杭州的车','A',0,1000,True))
            assert await asyncio.to_thread(first_started.wait,1)
            await voice.accept(Transcript('不对，改成后天','A',1000,2000,True))
            await asyncio.wait_for(voice.queue.join(),3)
            release.set()
            await asyncio.wait_for(voice.turn_queue.join(),3)
            assert len(process.cognition.requests)==1
            assert '改成后天' in process.cognition.requests[0].input_text
        finally:
            release.set()
            await voice.close()
    asyncio.run(scenario())


def test_native_http_uses_bearer_and_single_endpoint():
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    async def scenario():
        received=[]
        async def handler(request):
            received.append((request.path,request.headers.get('Authorization'),await request.json()))
            return web.json_response({'model':'fixture','answers':answers(COLLISION,{'relation':'background','old_work':'continue'})})
        app=web.Application();app.router.add_post('/v1/systemone',handler)
        async with TestServer(app) as server:
            port=SystemOnePort('bailian','fixture',str(server.make_url('/v1/systemone')),'test-secret')
            try:
                result=await port.decide(DecisionRequest('collision',{},COLLISION),deadline=monotonic()+1)
                assert result.status=='success'
                assert len(received)==1 and received[0][:2]==('/v1/systemone','Bearer test-secret')
            finally:await port.close()
    asyncio.run(scenario())
