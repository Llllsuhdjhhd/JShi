"""New protocol behavior, including calls, controls, scope and durable coverage."""
from copy import deepcopy
import json
from time import time

import pytest

from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV
from jshi.voice.jev_material import support_evidence
from jshi.voice.jev_scene import JEVSceneStore, recent_material
from tests.test_voice import process, FakeCloud, Model as MainModel


def entry(route='prepare', control='redirect', **changes):
    data = {'items': [{'i': 1, 'person': 'P1', 'certainty': 'confirmed', 'why': '前句确认，发言连续'}],
            'interaction': 'direct', 'utterance_complete': 'complete', 'control': control, 'route': route,
            'process_refs': ['I1'] if route == 'prepare' else [],
            'pending_refs': ['I1'] if route == 'wait' else [],
            'decision_reason': '当前明确向匠石交往', 'decision_refs': ['I1']}
    data.update(changes)
    return data


def preparation(**changes):
    data = {'route': 'main', 'level': 2, 'recall_memory': False,
            'main_image': 'omit', 'reply': '', 'waiting_reply': '', 'reason': '需要展开理解'}
    data.update(changes)
    return data


class Model:
    def __init__(self, answers):
        self.answers, self.requests = list(answers), []

    def generate(self, request):
        self.requests.append(request)
        if request.purpose == 'voice_jev_scene':
            return ModelResponse(text=json.dumps({'scene':'【话题：交往】本批已处理。'}), model='fixture')
        result = self.answers.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result(request)
        return ModelResponse(text=json.dumps(result, ensure_ascii=False), model='fixture')


def run(mode, first=None, second=None, **kwargs):
    first = first or entry()
    second = second or preparation()
    if mode == 'combined':
        first = {**first, 'preparation': second if first['route'] == 'prepare' else None}
        answers = [first]
    else:
        answers = [first, second]
    model = Model(answers)
    gate = VoiceJEV(model, mode=mode, fallback_models=[])
    batch = [{'n': 1, 'who': 'P1', 'text': '请解释一下', 'at': time()*1000,
              'voice_evidence': {'pick': 'P1', 'strength': 'weak'},
              'candidates': [{'who': 'P1', 'score': 1., 'source': 'personal_classifier', 'verified': False}]}]
    delivery = kwargs.pop('delivery', {'state': 'completed', 'now': time()*1000,
                                     'jev_scene': '【话题：家庭讨论】P1在连续讲述。'})
    result = gate.decide_batch('stone', batch, [], delivery, **kwargs)
    return result, model


@pytest.mark.parametrize('mode', ['split', 'combined'])
def test_same_decision_and_only_needed_calls(mode):
    result, model = run(mode)
    assert result.judged and result.route == 'main' and not result.model_error
    assert result.main_prompt_hint['level'] == 2
    assert len(model.requests) == (2 if mode == 'split' else 1)
    assert '【当前现场·JEV2已编排】' in model.requests[0].input_text
    assert 'classification_score' not in model.requests[0].input_text
    assert '未分档' in model.requests[0].input_text
    if mode == 'split':
        assert model.requests[0].purpose == 'voice_jev_entry'
        assert model.requests[1].purpose == 'voice_jev_prepare'
        assert '人物证据' not in model.requests[1].input_text


@pytest.mark.parametrize('mode', ['split', 'combined'])
@pytest.mark.parametrize('first', [entry('background', 'keep', interaction='none'),
                                 entry('wait', 'keep', utterance_complete='incomplete'),
                                 entry('background', 'stop')])
def test_background_wait_and_stop_skip_preparation(mode, first):
    result, model = run(mode, first)
    assert result.route == first['route'] and not result.direct_reply
    assert len(model.requests) == 1
    assert result.action == ('stop' if first['control'] == 'stop' else 'ignore')


def test_control_applied_before_second_call_and_survives_failure():
    controls = []
    def fail(_):
        assert controls == ['redirect']
        raise RuntimeError('second-stage failure')
    model = Model([entry(), fail])
    gate = VoiceJEV(model, mode='split', fallback_models=[])
    result = gate.decide_batch('stone', [{'n': 1, 'who': 'P1', 'text': '换个话题'}], [], {},
                              on_entry=lambda e: controls.append(e.control))
    assert result.judged and result.items[0].speaker_pick == 'P1'
    assert result.control == 'redirect' and result.action == 'ignore' and result.model_error
    assert not result.direct_reply and not result.waiting_reply


def test_stop_and_independent_operation_is_silent_l1():
    result, _ = run('split', entry(control='stop'), preparation(level=3))
    assert result.action == 'respond' and result.silent_operation
    assert result.main_prompt_hint['level'] == 1 and result.route == 'main'
    assert not result.direct_reply and not result.waiting_reply


def test_invalid_refs_never_apply_control():
    applied = []
    result, model = run('split', entry(decision_refs=['I99']), on_entry=lambda e: applied.append(e))
    assert result.model_error and not applied and len(model.requests) == 1


def test_stale_preparation_never_speaks():
    state = {'current': True}
    def second(_):
        state['current'] = False
        return preparation(route='self_reply', level=3, reply='你好')
    model = Model([entry(), second])
    gate = VoiceJEV(model, mode='split', fallback_models=[])
    result = gate.decide_batch('stone', [{'n': 1, 'who': 'P1', 'text': '你好'}], [],
                              {'short_chat': {'allowed': True}}, is_current=lambda: state['current'])
    assert result.model_error and not result.direct_reply


@pytest.mark.parametrize('mode', ['split', 'combined'])
def test_direct_answer_permission_checked_by_program(mode):
    result, _ = run(mode, second=preparation(route='self_reply', level=3, reply='你好'))
    assert result.route == 'main' and not result.direct_reply
    result, _ = run(mode, second=preparation(route='self_reply', level=3, reply='你好'),
                    delivery={'short_chat': {'allowed': True, 'person_code': 'P1'}})
    assert result.route == 'self_reply' and result.direct_reply == '你好'


def test_mixed_completeness_keeps_identity_but_only_passes_complete_part():
    first = entry(utterance_complete='uncertain', pending_refs=['I2'])
    first['items'].append({'i': 2, 'person': 'unknown', 'certainty': 'unknown', 'why': '没有有效证据'})
    model = Model([first, preparation()])
    gate = VoiceJEV(model, mode='split', fallback_models=[])
    result = gate.decide_batch('stone', [{'n': 1, 'who': 'P1', 'text': '解释一下'},
        {'n': 2, 'text': '还有那个……'}], [], {})
    assert result.items[0].keep and not result.items[1].keep
    assert result.pending_refs == ('I2',) and result.process_refs == ('I1',)
    # The pending reference is provided, but not its unfinished wording.
    assert '还有那个' not in model.requests[1].input_text


def test_support_requires_calibration_matched_to_source_and_model():
    candidate = {'p': 'P1', 'score': 1., 'source': 'personal_classifier', 'model_version': 'v1'}
    assert support_evidence(candidate)['support'] == '未分档'

    candidate['support_calibration'] = {'model_version': 'v1', 'source': 'personal_classifier',
                                      'validated': True, 'boundaries': [.12, .31, .76, .98]}
    assert support_evidence(candidate)['support'] == '强支持'
    candidate['model_version'] = 'v2'
    assert support_evidence(candidate)['support'] == '未分档'


def test_task_reference_and_clock_survive_shared_snapshot():
    from jshi.voice.jev_input import compact_jev_input
    source = {'now': 200000, 'task_context': [{'ref':'Q1','updated_at':190.,'purpose':'取消下载'}]}
    result = compact_jev_input(source)
    assert result['task_context'][0]['ref'] == 'Q1'
    assert result['task_context'][0]['updated_at'] == -10000
    assert source['task_context'][0]['updated_at'] == 190.


def test_old_uncovered_event_survives_idle_window(tmp_path):
    store = JEVSceneStore(tmp_path/'scene.json')
    now = time()*1000
    store.append('old', {'at': now-1800000, 'text': '等大家到齐再开始'})
    material = store.material_view(now)
    assert material['events'][0]['text'] == '等大家到齐再开始'


def test_archived_is_not_understood_and_revised_event_reenters(tmp_path):
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('long', {'text': 'a'*4000, 'at': time()*1000})
    store.append('short', {'text': '此前暂归', 'at': time()*1000})
    snapshot = store.snapshot()
    recent_material(snapshot, '')
    assert store.commit(snapshot, '【话题：讨论】人物暂归。')
    state = store.snapshot()
    assert 'long' not in state['covered'] and 'long' in state['archived_unread']
    assert 'short' in state['covered']
    store.append('short', {'text': '原话修订', 'at': time()*1000})
    state = store.snapshot()
    assert 'short' not in state['covered'] and any(r['id'] == 'short' for r in state['events'])
    assert state['event_versions']['short'] == 2


@pytest.mark.parametrize('mode', ['split', 'combined'])
def test_voice_pipeline_runs_main_once_in_both_modes(process, mode):
    import asyncio
    from jshi.app.voice import VoiceConversation
    from jshi.voice.volc import Transcript
    from jshi.models import ResponsePlan
    first = entry(items=[{'i': 1, 'person': 'unknown', 'certainty': 'unknown', 'why': '声音证据不足，交往明确'}])
    answers = [first, preparation()] if mode == 'split' else [{**first, 'preparation': preparation()}]
    model = Model(answers)
    class Quiet(MainModel):
        def generate(self, request):
            self.requests.append(request)
            return ModelResponse(model='quiet', response_plan=ResponsePlan(mode='ignore'))
    process.cognition = Quiet()
    process.style_packs.set('stone', 'smith')
    async def scenario():
        async def send(message):
            pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send,
                              jev=VoiceJEV(model, mode=mode, fallback_models=[]))
        try:
            await c.accept(Transcript('匠石，解释一下', 'A', 0, 1000, True))
            await asyncio.wait_for(c.queue.join(), 4)
            await asyncio.wait_for(c.turn_queue.join(), 4)
            assert len(process.cognition.requests) == 1
            assert c.epoch == 1  # redirect applied once, before preparation
            assert len([r for r in model.requests if r.purpose != 'voice_jev_scene']) == (2 if mode == 'split' else 1)
        finally:
            await c.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('invalidate', ['stop', 'revision'])
def test_voice_applies_control_while_preparation_still_running(process, invalidate):
    import asyncio
    from threading import Event
    from jshi.app.voice import VoiceConversation
    from jshi.core.envelope import SceneUtterance, SpeakerEvidence
    started, release = Event(), Event()
    first = entry(items=[{'i': 1, 'person': 'unknown', 'certainty': 'unknown', 'why': '身份未知但改向明确'}])
    def second(_):
        started.set()
        assert release.wait(3)
        return preparation()
    async def scenario():
        async def send(message):
            pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send,
                              jev=VoiceJEV(Model([first, second]), mode='split', fallback_models=[]))
        task = None
        try:
            speaker = SpeakerEvidence('A')
            utterance = SceneUtterance('换个话题', speaker, 0, 1000, input_id='a', received_at_ms=time()*1000)
            task = asyncio.create_task(c._judge((utterance,), speaker, False))
            async def wait_started():
                while not started.is_set():
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait_started(), 2)
            assert c.epoch == 1 and not task.done()
            if invalidate == 'stop':
                await c.stop('explicit stop')
            else:
                from types import SimpleNamespace
                c.input_records['a'] = {'transcript': SimpleNamespace(text='修订后的原话')}
            release.set()
            decision, record, _ = await asyncio.wait_for(task, 2)
            assert decision.action == 'ignore' and record['route'] == 'background'
            assert not record['direct_reply'] and not record['waiting_reply']
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await c.close()
    asyncio.run(scenario())


def test_mode_switch_persists_and_reuses_model_route(tmp_path):
    from jshi.app.model_settings import ModelSettings
    from tests.test_model_settings import process as settings_process
    gate = VoiceJEV(Model([]), mode='split')
    settings = ModelSettings(tmp_path, settings_process(tmp_path), gate)
    model = gate.model
    assert settings.save({'jev_mode': 'combined'})['jev_mode'] == 'combined'
    assert gate.mode == 'combined' and gate.model is model
    restored = VoiceJEV(Model([]), mode='split')
    ModelSettings(tmp_path, settings_process(tmp_path), restored)
    assert restored.mode == 'combined'
    with pytest.raises(ValueError):
        settings.save({'jev_mode': 'parallel'})


def test_scene_can_supply_identity_candidate_without_voice_match():
    model = Model([entry(), preparation()])
    result = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch('stone',
        [{'n':1,'text':'这一步还要加盐','who':'unknown','candidates':[]}], [],
        {'jev_scene':'【话题：做饭】P1持续讲述，前句已确认。','people':{'P1':'小林'}})
    assert result.items[0].speaker_pick == 'P1' and result.judged and not result.model_error


def test_silent_operation_runs_main_but_never_delivers_its_reply(process):
    from dataclasses import replace
    from tests.test_voice import envelope
    from jshi.models import ResponsePlan, ResponseItem, ToolUseIntent
    seen = []
    class Main(MainModel):
        def generate(self, request):
            self.requests.append(request)
            return ModelResponse(model='main', response_plan=ResponsePlan('respond', items=(ResponseItem('verbal','已经取消'),)),
                                 tool_intent=ToolUseIntent(need='取消下载'))
    process.cognition = Main()
    incoming, identities = envelope(process, '别说了，把下载取消')
    incoming = replace(incoming, jev_calls=({'silent_operation':True,
                      'main_prompt_hint':{'level':1,'recall_memory':False}},))
    process._intake_tool_if_requested = lambda *args: seen.append(args[3].tool_intent.need)
    spoken = []
    result = process.experience('stone', incoming.text, envelope=incoming,
        resolved_speaker=identities.candidate(incoming.speaker), on_verbal=spoken.append, defer_write=True)
    assert len(process.cognition.requests) == 1 and seen == ['取消下载']
    assert not result.action_text and not result.response_plan.verbal_text() and not spoken


@pytest.mark.parametrize('mode', ['split', 'combined'])
def test_standalone_text_logs_both_stages_and_preserves_person_scope(process, mode):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    from jshi.app.web_data import WebData
    first = entry()
    answers = [first, preparation()] if mode == 'split' else [{**first,'preparation':preparation()}]
    model = Model(answers)
    gate = VoiceJEV(model, mode=mode, fallback_models=[])
    async def scenario():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig(api_key=''),jev=gate))) as client:
            origin = str(client.make_url('')).rstrip('/')
            response = await client.post('/text-input', headers={'Origin':origin},
                                         json={'text':'请解释一下','object_id':'lux-id'})
            assert response.status == 200, await response.text()
            data = await response.json()
            assert data['label'] == 'lux'
            assert len(process.cognition.requests) == 1
    asyncio.run(scenario())
    calls = [r for r in process.step_inputs._read() if r.get('kind') == 'call']
    expected = {'voice_jev_entry','voice_jev_prepare'} if mode == 'split' else {'voice_jev'}
    assert expected <= {r['purpose'] for r in calls}
    rendered = WebData(process.repository.path.parent,'stone').diagnostics(section='prompt',purpose='voice_jev')
    assert '【当前输入】' in rendered['text']
    if mode == 'split':
        assert '【入口结论】' in rendered['text']
    scenes = list((process.repository.path.parent/'jev_scenes').rglob('text-entry.json'))
    assert scenes and JEVSceneStore(scenes[0]).snapshot()['codes']['P1'] == 'lux-id'


def test_standalone_failed_preparation_falls_back_to_main(process):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    gate = VoiceJEV(Model([entry(), RuntimeError('preparation offline')]), mode='split', fallback_models=[])
    async def scenario():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig(api_key=''),jev=gate))) as client:
            origin = str(client.make_url('')).rstrip('/')
            response = await client.post('/text-input',headers={'Origin':origin},
                                         json={'text':'请解释一下','object_id':'lux-id'})
            assert response.status == 200
            data = await response.json()
            assert data['reply'] and data['route'] == 'main'
            assert process.cognition.requests
            assert data['timing']['end_to_end']['jev_fallback'] is True
            assert data['timing']['end_to_end']['jev_failure_kind'] == 'model_call'
    asyncio.run(scenario())
