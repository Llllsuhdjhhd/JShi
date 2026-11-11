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
    data = {'route': 'main', 'level': 2, 'recall_memory': False, 'recall_query': '',
            'main_image': 'omit', 'reply': '', 'waiting_reply': '', 'reason': '需要展开理解'}
    data.update(changes)
    return data


def main_preparation(**changes):
    return {k: v for k, v in preparation(**changes).items() if k not in {'route', 'reply'}}


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
        # Speech belongs to the first response; stage two supplies only needs.
        if isinstance(second, dict) and (second.get('route') == 'self_reply' or second.get('waiting_reply')):
            first = {**first, 'front': second}
        answers = [first, {k: v for k, v in second.items() if k not in {'route', 'reply'}}
                   if isinstance(second, dict) else second]
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
        assert 'short_chat' not in model.requests[1].input_text
        assert 'waiting' in model.requests[1].input_text


def test_split_second_stage_rejects_legacy_speech_fields():
    model = Model([entry(), preparation(route='self_reply', level=3, reply='不能在第二阶段说的话')])
    result = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch(
        'stone', [{'n': 1, 'who': 'P1', 'text': '请解释一下'}], [],
        {'state': 'completed', 'short_chat': {'allowed': True, 'person_code': 'P1'}})
    assert result.model_error and not result.direct_reply and not result.waiting_reply


def test_history_retains_fallible_addressee_judgment_separately_from_identity():
    from jshi.voice.scene_material import dialogue_row
    row = dialogue_row({'p': 'P1', 'text': '几点走', 'entry_interaction': 'none',
                        'judgment': {'person': 'P1', 'certainty': '确定'}})
    assert row['entry_interaction'] == 'none'
    assert row['identity']['certainty'] == '确定'


def test_revision_alternative_reaches_entry_and_main_without_becoming_new_speech():
    from jshi.voice.jev_material import support_for_main
    model = Model([entry(front=preparation())])
    result = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch(
        'stone', [{'n': 1, 'who': 'P1', 'text': 'You should get enough sleep.',
                   'asr_previous': ['你看是不是会背了？']}], [], {'state': 'completed'})
    assert not result.model_error
    assert '你看是不是会背了？' in model.requests[0].input_text
    assert any('后到不保证更准确' in note for row in result.entry_support for note in row['notes'])


@pytest.mark.parametrize('mode', ['split', 'combined'])
@pytest.mark.parametrize('first', [entry('background', 'keep', interaction='none'),
                                 entry('wait', 'keep', utterance_complete='incomplete'),
                                 entry('background', 'stop')])
def test_background_wait_and_stop_skip_preparation(mode, first):
    result, model = run(mode, first)
    assert result.route == first['route'] and not result.direct_reply
    assert len(model.requests) == 1
    assert result.action == ('stop' if first['control'] == 'stop' else 'ignore')


@pytest.mark.parametrize('mode',['split','combined'])
def test_positive_continue_never_applies_stop_even_if_model_returns_stop(mode):
    first=entry(route='background',control='stop')
    if mode=='combined':first={**first,'preparation':None}
    retry = main_preparation() if mode == 'split' else preparation(route='self_reply',level=3,reply='好，我接着说。')
    model=Model([first,retry])
    gate=VoiceJEV(model,mode=mode,fallback_models=[])
    controls=[]
    result=gate.decide_batch('stone',[{'n':1,'who':'P1','text':'就这么停啊？你给他问他一个，你继续啊。'}],[],
        {'state':'completed','short_chat':{'allowed':True,'person_code':'P1'}},
        on_entry=lambda e:controls.append(e.control))
    assert result.control!='stop' and 'stop' not in controls
    assert not result.model_error
    if mode == 'split':
        assert result.route == 'main' and not result.direct_reply
    else:
        assert result.direct_reply == '好，我接着说。'


@pytest.mark.parametrize('text',['别继续啊','不要继续说','不要你继续','我不希望你继续','他说“你继续啊”','你继续啊，别说了'])
def test_stop_guard_does_not_override_negation_or_quotation(text):
    from jshi.voice.jev_serial import protect_continuation
    original=entry(route='background',control='stop')
    assert protect_continuation(original,[{'n':1,'text':text}])==(original,False)


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


@pytest.mark.parametrize('recall,query,expected', [(True, '上周的出行约定', '上周的出行约定'), (False, '不该带上', None)])
def test_recall_query_reaches_main_hint_only_when_recall_is_requested(recall, query, expected):
    result, _ = run('split', entry(), preparation(recall_memory=recall, recall_query=query))
    assert result.main_prompt_hint.get('recall_query') == expected


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


@pytest.mark.parametrize('mode', ['split','combined'])
@pytest.mark.parametrize('scope,person,recent,allowed', [
    ('general',None,[],True), ('person',{'name':'其他人'},[],False),
    ('general',{'name':'其他人'},[],False), ('general',None,[{'reply':'旧人物经历'}],False)])
def test_general_short_reply_survives_attribution_but_cannot_borrow_personal_context(mode,scope,person,recent,allowed):
    first=entry(items=[{'i':1,'person':'P1','certainty':'tentative','why':'仅候选'}])
    result,_=run(mode,first,preparation(route='self_reply',level=3,reply='我在，你说。'),
        delivery={'short_chat':{'allowed':True,'person_code':'P2','scope':scope,'person':person,'recent':recent}})
    assert bool(result.direct_reply) is allowed


@pytest.mark.parametrize('mode', ['split','combined'])
def test_exact_transport_marker_is_not_a_decision_field(mode):
    first=entry()
    second=preparation()
    if mode=='split':second['type']='json_object'
    else:first['type']='json_object'
    result,_=run(mode,first,second)
    assert result.route=='main' and not result.model_error
    assert any(s.get('normalization')=='transport_json_object_marker' for s in result.stage_records)
    first=entry(type='other')
    result,_=run(mode,first,preparation())
    assert result.model_error and not result.direct_reply


@pytest.mark.parametrize('mode', ['split', 'combined'])
@pytest.mark.parametrize('certainty,person,spoken', [('tentative', 'P1', '你好'), ('unknown', 'unknown', '')])
def test_tentative_person_may_get_short_reply_but_unknown_may_not(mode, certainty, person, spoken):
    first = entry(items=[{'i': 1, 'person': person, 'certainty': certainty, 'why': '声纹未分档'}])
    result, _ = run(mode, first, preparation(route='self_reply', level=3, reply='你好'),
                    delivery={'short_chat': {'allowed': True, 'person_code': 'P1'}})
    assert result.direct_reply == spoken
    assert result.route == ('self_reply' if spoken else 'main')


def test_mixed_completeness_keeps_identity_but_only_passes_complete_part():
    first = entry(utterance_complete='uncertain', pending_refs=['I2'])
    first['items'].append({'i': 2, 'person': 'unknown', 'certainty': 'unknown', 'why': '没有有效证据'})
    model = Model([first, main_preparation()])
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
    assert result['task_context'][0]['updated_at'] == -10
    assert source['task_context'][0]['updated_at'] == 190.


def test_snapshot_merges_duplicate_history_views_without_merging_people_or_times():
    from jshi.voice.jev_material import build_snapshot
    payload = {'input': {'lines': [], 'unwritten': [
        {'ref':'E1','p':'P1','text':'你看得到吗？','at':0}]},
        'conversation': [
            {'evidence_ref':'C1','who':'qf','text':'你看得到吗？','at':0},
            {'evidence_ref':'C2','who':'P2','text':'你看得到吗？','at':0},
            {'evidence_ref':'C3','who':'P1','text':'你看得到吗？','at':-1000}]}
    events=build_snapshot(payload, [], {'named_people':{'P1':'qf'}})['events']
    assert len(events)==3
    assert events[1]['ref']=='E1'
    assert [r.get('evidence_ref') for r in events]==['C3',None,'C2']


def test_latest_human_question_follows_older_assistant_question_across_history_views():
    from jshi.voice.jev_material import build_snapshot
    payload={'input':{'lines':[],'unwritten':[
        {'ref':'E10','p':'lux','text':'你今天还玩游戏，是吧？','at':-3374}]},
        'conversation':[{'evidence_ref':'C5','who':'匠石','text':'有什么事？','at':-15471},
                        {'evidence_ref':'C6','who':'qf','text':'时间未提供','at':None}]}
    events=build_snapshot(payload,[],{})['events']
    assert [r['text'] for r in events[:2]]==['有什么事？','你今天还玩游戏，是吧？']
    assert events[-1]['at'] is None


def test_ambient_question_needs_interaction_evidence_but_keeps_real_continuations():
    from jshi.voice.jev_serial import voice_interaction_has_basis
    base={'lines':[{'source':'voice','text':'你看得到吗？'}],
          'state':{'state':'completed','names':['匠石']},'events':[],
          'availability':{'image_available':False},'environment':None}
    assert not voice_interaction_has_basis(base)
    for change in [
        {'lines':[{'source':'voice','text':'匠石，你看得到吗？'}]},
        {'events':[{'p':'P1','text':'匠石，先听我说。','basis':'收到的原话'}]},
        {'availability':{'image_available':True}},
        {'state':{'state':'playing','names':['匠石'],'interaction_origin':{'text':'你好','basis':'有效面对面交往'}}},
        {'lines':[{'source':'web_text','text':'你看得到吗？'}]}]:
        assert voice_interaction_has_basis({**base,**change})
    assert not voice_interaction_has_basis({**base,'environment':{'status':'description_stale','description':'面对电脑'}})
    assert not voice_interaction_has_basis({**base,'events':[{'p':'匠石','text':'你想让我看什么？','basis':'已播'}],
        'scene':'匠石和此人持续交谈，匠石说能调参数。',
        'state':{'state':'playing','spoken':'我帮你调','names':['匠石']}})
    assert not voice_interaction_has_basis({**base,'events':[{'p':'P1','text':'匠石，先听我说。',
        'basis':'收到的原话','scope':'previous_session'}]})


@pytest.mark.parametrize('mode',['split','combined'])
def test_unfounded_ambient_direct_result_never_generates_or_applies_redirect(mode):
    first=entry()
    if mode=='combined':first={**first,'preparation':preparation()}
    model=Model([first])
    controls=[]
    result=VoiceJEV(model,mode=mode,fallback_models=[]).decide_batch('stone',
        [{'n':1,'who':'P1','source':'voice','text':'你看得到吗？'}],[],
        {'state':'completed','now':time()*1000},on_entry=lambda r:controls.append(r.control))
    assert result.route=='background' and result.control=='keep'
    assert len(model.requests)==1 and controls==['keep']
    assert any(r['stage']=='interaction_grounding' for r in result.stage_records)


def test_old_self_interjections_cannot_bootstrap_another_short_reply():
    first = entry(front=preparation(route='self_reply', level=3, reply='可以'))
    model = Model([first])
    controls = []
    result = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch('stone',
        [{'n': 1, 'who': 'P1', 'source': 'voice', 'text': '这样调可以吧？'}],
        [{'p': '匠石', 'text': '我按这个顺序调整。', 'basis': '已播', 'at': time()*1000-1000}],
        {'state': 'completed', 'spoken': '我按这个顺序调整。', 'now': time()*1000,
         'jev_scene': '匠石承诺调整，P1持续讨论参数。',
         'short_chat': {'allowed': True, 'scope': 'general', 'person': None, 'recent': []}},
        on_entry=lambda r: controls.append(r.control))
    assert result.route == 'background' and not result.direct_reply
    assert controls == ['keep'] and len(model.requests) == 1


@pytest.mark.parametrize('scope,expected', [('general', 'self_reply'), ('person', 'main')])
def test_split_anonymous_single_addressee_can_only_use_general_material(scope, expected):
    first = entry(items=[{'i': 1, 'person': 'unknown', 'certainty': 'unknown', 'why': '未确认姓名'}],
                  front=preparation(route='self_reply', level=3, reply='我在。'))
    result, model = run('split', first, delivery={'state': 'completed', 'now': time()*1000,
        'short_chat': {'allowed': True, 'scope': scope, 'person': None, 'recent': [], 'person_code': 'P1'}})
    assert result.route == expected and len(model.requests) == 1
    assert result.items[0].speaker_pick == 'unknown' and result.items[0].speaker_level == '不确定'
    assert bool(result.direct_reply) == (scope == 'general')


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
    answers = [first, main_preparation()] if mode == 'split' else [{**first, 'preparation': preparation()}]
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
            assert all(r.get('entry_interaction') == 'direct' for r in c.input_records.values())
            snapshot = c.jev_scene_store.snapshot()
            captured = snapshot['events'] + [e for r in snapshot['revisions'] for e in r['events']]
            assert any(e['payload'].get('entry_interaction') == 'direct' for e in captured)
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
            utterance = SceneUtterance('匠石，换个话题', speaker, 0, 1000, input_id='a', received_at_ms=time()*1000)
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
    model = Model([entry(), main_preparation()])
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


@pytest.mark.parametrize('mode', ['split', 'combined'])
def test_stop_survives_invalid_background_process_scope(mode):
    controls = []
    def apply(value):
        controls.append(value.control)
        return 7
    # Reproduces 2026-11-03 19:18:57: stop was correct, process_refs was redundant.
    first = entry('background', 'stop', process_refs=['I1'])
    first['items'][0].update(person='unknown', certainty='unknown', why='姓名未知')
    result, model = run(mode, first, on_entry=apply)
    assert controls == ['stop']
    assert result.action == result.control == 'stop' and result.control_applied
    assert result.applied_epoch == 7 and result.routing_error
    assert not result.direct_reply and not result.waiting_reply and len(model.requests) == 1


def test_rejected_stop_receipt_does_not_promote_failed_route():
    result, _ = run('split', entry('background', 'stop', process_refs=['I1']),
                    on_entry=lambda value: False)
    assert result.control != 'stop' and not result.control_applied


def test_invalid_stop_reference_cannot_reach_control_callback():
    applied = []
    result, _ = run('split', entry('background', 'stop', process_refs=['I1'], decision_refs=['I99']),
                    on_entry=lambda value: applied.append(value))
    assert not applied and result.control != 'stop'


def test_stale_stop_does_not_reach_control_callback():
    state = {'current': True}
    def response(request):
        state['current'] = False
        return entry('background', 'stop', process_refs=['I1'])
    applied = []
    model = Model([response])
    result = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch(
        'stone', [{'n':1,'text':'不用解释了'}], [], {},
        is_current=lambda: state['current'], on_entry=lambda value: applied.append(value))
    assert not applied and result.control != 'stop'


@pytest.mark.parametrize('mode', ['split', 'combined'])
@pytest.mark.parametrize('bad_control', ['prepare', None, [], False])
def test_invalid_control_defaults_only_when_idle(mode, bad_control):
    result, model = run(mode, entry(control=bad_control))
    assert result.judged and not result.model_error and result.control == 'keep'
    assert len(model.requests) == (2 if mode == 'split' else 1)
    assert result.stage_records[0]['control_fallback']['applied'] is True
    assert json.loads(result.stage_records[0]['raw'])['control'] == bad_control


@pytest.mark.parametrize('state', ['thinking', 'playing', 'paused', 'queued', 'unknown'])
@pytest.mark.parametrize('mode', ['split', 'combined'])
@pytest.mark.parametrize('recovers', [True, False])
def test_busy_control_error_retries_once_before_any_effect(state, mode, recovers):
    invalid = entry(control='prepare')
    valid = entry(route='background', control='keep', interaction='none')
    if mode == 'combined':
        invalid = {**invalid, 'preparation': preparation()}
        valid = {**valid, 'preparation': None}
    model = Model([invalid, valid if recovers else invalid])
    gate = VoiceJEV(model, mode=mode, fallback_models=[])
    controls = []
    result = gate.decide_batch('stone', [{'n': 1, 'who': 'P1', 'text': '你好'}], [],
                              {'state': state}, on_entry=lambda r: controls.append(r.control))
    assert len(model.requests) == 2
    assert result.stage_records[0]['status'] == 'retried'
    assert result.stage_records[0]['control_fallback']['applied'] is False
    assert controls == (['keep'] if recovers else [])
    assert bool(result.model_error) is (not recovers)
    assert not result.direct_reply and result.control == 'keep'


@pytest.mark.parametrize('change', [{'route': 'oops'}, {'decision_refs': ['MISSING']},
                                   {'extra': True}, {'utterance_complete': 123}])
def test_control_default_does_not_hide_other_schema_errors(change):
    result, model = run('split', entry(control='prepare', **change))
    assert result.model_error and not result.direct_reply
    assert len(model.requests) == 1


def test_control_default_does_not_hide_route_conflict():
    result, model = run('split', entry(control='prepare', utterance_complete='incomplete'))
    assert result.model_error and not result.direct_reply
    assert len(model.requests) == 1


@pytest.mark.parametrize('mode',['split','combined'])
def test_missing_ambient_addressee_does_not_swallow_stop_and_cancel_operation(mode):
    first=entry(control='stop')
    second=preparation(level=1)
    model=Model([{**first,'preparation':second}] if mode=='combined' else [first,main_preparation(level=1)])
    controls=[]
    result=VoiceJEV(model,mode=mode,fallback_models=[]).decide_batch('stone',
        [{'n':1,'who':'P1','text':'别说了，把下载也取消','source':'voice'}],[],
        {'state':'completed'},on_entry=lambda value:controls.append(value.control))
    assert controls==['stop'] and result.silent_operation and result.route=='main'
    assert not result.direct_reply and not result.waiting_reply

@pytest.mark.parametrize('text', ['我有点累，只想安静一会儿。', '给我一个放松的小建议。'])
def test_split_first_call_can_answer_beyond_greetings(text):
    first=entry(front=preparation(route='self_reply',level=3,reply='先放松肩膀，慢慢呼吸。'))
    model=Model([first])
    result=VoiceJEV(model,mode='split',fallback_models=[]).decide_batch('stone',
        [{'n':1,'who':'P1','source':'web_text','text':text}],[],
        {'state':'completed','short_chat':{'allowed':True,'person_code':'P1'}})
    assert not result.model_error and result.direct_reply=='先放松肩膀，慢慢呼吸。'
    assert len(model.requests)==1 and model.requests[0].purpose=='voice_jev_entry'


def test_split_first_call_can_prepare_main_and_waiting_reply():
    first=entry(front=preparation(level=1,waiting_reply='我核对一下。'))
    model=Model([first])
    result=VoiceJEV(model,mode='split',fallback_models=[]).decide_batch('stone',
        [{'n':1,'who':'P1','source':'web_text','text':'查一下杭州这周天气'}],[],
        {'state':'completed','waiting':{'allowed':True,'levels':{'L1':{'expected_seconds':3,'max_chars':16}}}})
    assert not result.model_error and result.route=='main'
    assert result.waiting_reply=='我核对一下。' and len(model.requests)==1


def test_combined_does_not_accept_split_front_field():
    result,model=run('combined',entry(front=preparation()))
    assert result.model_error and not result.direct_reply


@pytest.mark.parametrize('state,control,overlap', [('playing','redirect',False),('completed','stop',False),('completed','redirect',True)])
def test_split_front_cannot_skip_preparation_during_control_or_overlap(state,control,overlap):
    result,model=run('split',entry(control=control,front=preparation(route='self_reply',level=3,reply='不能提前说出')),
                     preparation(level=1),delivery={'state':state},overlap=overlap)
    assert not result.model_error and not result.direct_reply
    assert len(model.requests)==2 and model.requests[1].purpose=='voice_jev_prepare'


def test_split_front_cannot_use_another_persons_cached_context():
    result,model=run('split',entry(front=preparation(route='self_reply',level=3,reply='不能提前说出')),
        delivery={'state':'completed','short_chat':{'allowed':True,'scope':'person','person_code':'P2','person':{'name':'其他人'}}})
    assert not result.model_error and result.route=='main' and not result.direct_reply


@pytest.mark.parametrize('first_front', [None, 'omitted'])
def test_split_preparation_can_acknowledge_without_direct_reply(first_front):
    first = entry(**({'front': None} if first_front is None else {}))
    model = Model([first, main_preparation(recall_memory=True, waiting_reply='嗯，我回想一下。')])
    result = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch(
        'stone', [{'n': 1, 'who': 'P1', 'text': '你还记得我别的吗', 'source': 'web_text'}], [],
        {'state': 'completed', 'waiting': {'allowed': True, 'levels': {'L2': {'expected_seconds': 4, 'max_chars': 16}}}})
    assert not result.model_error and result.route == 'main'
    assert result.waiting_reply == '嗯，我回想一下。' and not result.direct_reply
    assert len(model.requests) == 2
    assert '"waiting":{"allowed":true' in model.requests[1].input_text
    assert 'short_chat' not in model.requests[1].input_text


@pytest.mark.parametrize('allowed,state,control,seconds,phrase', [
    (False, 'completed', 'keep', 4, '嗯……'),
    (True, 'playing', 'keep', 4, '嗯……'),
    (True, 'completed', 'keep', 1, '嗯……'),
    (True, 'completed', 'keep', 4, ''),
])
def test_second_stage_ack_keeps_existing_permissions(allowed, state, control, seconds, phrase):
    model = Model([entry(control=control), main_preparation(waiting_reply=phrase)])
    result = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch(
        'stone', [{'n': 1, 'who': 'P1', 'text': '你还记得别的吗', 'source': 'web_text'}], [],
        {'state': state, 'waiting': {'allowed': allowed, 'levels': {'L2': {'expected_seconds': seconds, 'max_chars': 16}}}})
    assert result.route == 'main' and not result.model_error and not result.waiting_reply
    assert len(model.requests) == 2


def test_busy_main_preparation_is_preserved_without_speech_or_extra_call():
    model = Model([entry(control='keep', front=preparation(recall_memory=True))])
    result = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch(
        'stone', [{'n': 1, 'who': 'P1', 'text': '还记得别的吗', 'source': 'web_text'}], [],
        {'state': 'playing', 'waiting': {'allowed': False}})
    assert result.route == 'main' and not result.model_error and result.main_prompt_hint['recall_memory']
    assert not result.direct_reply and not result.waiting_reply and len(model.requests) == 1


@pytest.mark.parametrize('phrase,expected', [('嗯，我翻翻。', '嗯，我翻翻。'), ('嗯……', '嗯……'), ('我再仔细想一下', ''), ('……', '')])
def test_ack_budget_counts_speech_without_discouraging_natural_pauses(phrase, expected):
    model = Model([entry(), main_preparation(level=3, recall_memory=True, waiting_reply=phrase)])
    result = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch(
        'stone', [{'n': 1, 'who': 'P1', 'text': '你还记得我别的吗', 'source': 'web_text'}], [],
        {'state': 'completed', 'waiting': {'allowed': True, 'levels': {'L3': {'expected_seconds': 2.2, 'max_chars': 4}}}})
    assert not result.model_error and result.waiting_reply == expected and result.route == 'main'
    assert len(model.requests) == 2
