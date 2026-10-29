"""Third mode: controls, identity, fallbacks, loop ownership and settings."""
import asyncio
from copy import copy, deepcopy
from time import monotonic
import json

import pytest

from jshi.decision.base import DecisionResult
from jshi.decision.entry_policy import ENTRY, entry_action
from jshi.decision.service import DecisionService, defaults
from jshi.voice.jev import VoiceJEV
from jshi.voice.jev_decision import entry_bridge
from tests.test_decision_runtime import answers
from tests.test_jev_split_modes import Model, entry, preparation
from tests.test_voice import process, FakeCloud, Model as MainModel


def decision(address='direct', expression='complete', effect='redirect', work='none'):
    return DecisionResult('success', answers(ENTRY, dict(address=address, expression=expression,
                           task_effect=effect, work_after_stop=work)), model='decision-fixture')


def run(result=None, *, bound=True, outputs=None, callback=None, current=None, batch=None):
    config = defaults()
    config['mode'] = 'active'
    result = result or decision()
    model = Model(outputs if outputs is not None else [preparation()])
    gate = VoiceJEV(model, mode='decision', fallback_models=[])
    batch = batch or [{'n': 1, 'who': 'P1', 'text': '请解释一下', 'source': 'web_text' if bound else 'audio',
                      'speaker': {'method': 'user_selected' if bound else 'voiceprint'},
                      'candidates': [{'who': 'P1'}]}]
    delivery = {'state': 'completed', '_decision_entry': lambda state, **kwargs: (result, config)}
    return gate.decide_batch('stone', batch, [], delivery, on_entry=callback, is_current=current), model


def test_bound_input_skips_identity_and_uses_preparation():
    value, model = run()
    assert not value.model_error and value.route == 'main'
    assert value.items[0].speaker_pick == 'P1'
    assert [r.purpose for r in model.requests] == ['voice_jev_prepare']
    assert value.stage_records[0]['stage'] == 'decision_entry'


def test_voice_identity_and_preparation_share_entry():
    identity = {'items': entry()['items'], 'source_conflict': False}
    value, model = run(bound=False, outputs=[identity, preparation()])
    assert not value.model_error and value.items[0].speaker_level == '确定'
    assert [r.purpose for r in model.requests] == ['voice_jev_identity', 'voice_jev_prepare']
    assert '不选择路由' in model.requests[0].system_extra


@pytest.mark.parametrize('expression,route', [('incomplete', 'wait'), ('complete', 'background')])
def test_wait_and_background_do_not_prepare(expression, route):
    value, model = run(decision(address='direct' if route == 'wait' else 'other',
                                expression=expression, effect='keep'), outputs=[])
    assert value.route == route and not value.model_error and not model.requests
    assert value.pending_refs == (('I1',) if route == 'wait' else ())


def test_stop_before_identity_and_no_duplicate_callback():
    calls = []
    def identity(request):
        assert len(calls) == 1 and calls[0].control == 'stop'
        return {'items': entry()['items'], 'source_conflict': False}
    def applied(value):
        calls.append(value)
        return 3
    value, _ = run(decision(effect='stop'), bound=False, outputs=[identity], callback=applied)
    assert value.control == 'stop' and value.control_applied and value.applied_epoch == 3
    assert len(calls) == 1 and not value.model_error


def test_stop_and_tool_operation_is_silent_l1():
    calls = []
    value, _ = run(decision(effect='stop', work='required'),
                   outputs=[preparation(waiting_reply='好的')], callback=lambda v: calls.append(v))
    assert len(calls) == 1 and value.silent_operation
    assert value.route == 'main' and value.main_prompt_hint['level'] == 1
    assert not value.direct_reply and not value.waiting_reply


def test_uncertain_falls_back_once_and_reuses_split_preparation():
    value, model = run(decision(address='uncertain'), outputs=[entry(), preparation()])
    assert value.route == 'main' and not value.model_error
    assert [r.purpose for r in model.requests] == ['voice_jev_entry', 'voice_jev_prepare']
    assert sum(r['stage'] == 'decision_fallback' for r in value.stage_records) == 1


def test_source_conflict_falls_back_without_restoring_stopped_answer():
    value, model = run(decision(effect='stop'), bound=False,
        outputs=[{'items': entry()['items'], 'source_conflict': True}, entry(), preparation()], callback=lambda v: 9)
    assert value.control == 'stop' and value.silent_operation and value.applied_epoch == 9
    assert not value.model_error and value.route == 'main'
    assert [r.purpose for r in model.requests] == ['voice_jev_identity', 'voice_jev_entry', 'voice_jev_prepare']


def test_stale_never_calls_identity_or_split():
    calls = 0
    def current():
        nonlocal calls
        calls += 1
        return calls == 1
    value, model = run(current=current)
    assert value.model_error and not model.requests


def test_multiple_inputs_do_not_call_decision_bridge():
    rows = [{'n': n, 'who': 'P1', 'text': '半句', 'candidates': [{'who': 'P1'}]} for n in (1, 2)]
    first = entry(items=entry()['items'] + [dict(entry()['items'][0], i=2)], process_refs=['I1', 'I2'])
    value, model = run(batch=rows, outputs=[first, preparation()])
    assert not value.model_error
    assert value.stage_records[0]['reason'] == 'unsupported_scope'
    assert model.requests[0].purpose == 'voice_jev_entry'


@pytest.mark.parametrize('provider', ['bailian', 'typesafe'])
def test_provider_specific_thresholds_and_missing_confidence(provider):
    config = defaults()
    data = decision()
    thresholds = config['entry_thresholds'][provider]
    assert entry_action(data, ['I1'], thresholds)['route'] == 'prepare'
    modified = deepcopy(data.answers)
    modified['task_effect'].pop('confidence')
    assert entry_action(DecisionResult('success', modified), ['I1'], thresholds) is None


def test_saved_old_settings_merge_new_defaults(tmp_path):
    old = defaults()
    for key in ('entry', 'entry_timeout_ms', 'entry_thresholds'):
        old.pop(key)
    old['provider'] = 'typesafe'
    (tmp_path/'decision_settings.local.json').write_text(json.dumps(old), encoding='utf8')
    config = DecisionService(tmp_path).config
    assert config['provider'] == 'typesafe' and config['entry'] is True


@pytest.mark.parametrize('mode', ['off', 'shadow', 'active'])
def test_bridge_uses_host_loop_and_frozen_configuration(tmp_path, mode):
    async def scenario():
        service = DecisionService(tmp_path)
        service.config['mode'] = mode
        calls = []
        async def decide(purpose, state, **kwargs):
            assert asyncio.get_running_loop() is loop
            calls.append((purpose, kwargs['config_snapshot']['mode']))
            return decision()
        service.decide = decide
        loop = asyncio.get_running_loop()
        bridge = entry_bridge(service)
        service.config['mode'] = 'off'
        result, config = await asyncio.to_thread(bridge, {'material': '合成测试'})
        await asyncio.sleep(.01)
        assert config['mode'] == mode
        assert len(calls) == (0 if mode == 'off' else 1)
        assert result.status == ('success' if mode == 'active' else 'unavailable')
        await service.close()
    asyncio.run(scenario())


def test_voice_host_runs_decision_identity_and_main_once(process):
    from jshi.app.voice import VoiceConversation
    from jshi.models import ModelResponse, ResponsePlan
    from jshi.voice.volc import Transcript
    class Quiet(MainModel):
        def generate(self, request):
            self.requests.append(request)
            return ModelResponse(model='quiet', response_plan=ResponsePlan(mode='ignore'))
    process.cognition = Quiet()
    process.style_packs.set('stone', 'smith')
    identity = {'items': [{'i': 1, 'person': 'unknown', 'certainty': 'unknown', 'why': '声音不足'}],
                'source_conflict': False}
    model = Model([identity, preparation()])
    async def scenario():
        async def send(message):
            pass
        voice = VoiceConversation(process, 'stone', FakeCloud(), send,
                                  jev=VoiceJEV(model, mode='decision', fallback_models=[]))
        voice.decisions.config['mode'] = 'active'
        calls = []
        async def decide(purpose, state, **kwargs):
            calls.append(purpose)
            return decision()
        voice.decisions.decide = decide
        try:
            await voice.accept(Transcript('匠石，解释一下', 'A', 0, 1000, True))
            await asyncio.wait_for(voice.queue.join(), 4)
            await asyncio.wait_for(voice.turn_queue.join(), 4)
            assert calls == ['entry'], [r.input_text for r in model.requests]
            assert len(process.cognition.requests) == 1 and voice.epoch == 1
            assert [r.purpose for r in model.requests if r.purpose != 'voice_jev_scene'] == [
                'voice_jev_identity', 'voice_jev_prepare']
        finally:
            await voice.close()
    asyncio.run(scenario())


def test_third_mode_persists_across_restart(tmp_path):
    from jshi.app.model_settings import ModelSettings
    from tests.test_model_settings import process as settings_process
    from jshi.models.base import EchoModel
    settings = ModelSettings(tmp_path, settings_process(tmp_path), VoiceJEV(EchoModel(), mode='split'))
    assert settings.save({'jev_mode': 'decision'})['jev_mode'] == 'decision'
    gate = VoiceJEV(EchoModel(), mode='split')
    restored = ModelSettings(tmp_path, settings_process(tmp_path), gate)
    assert restored.public()['jev_mode'] == gate.mode == 'decision'


def test_selected_text_http_uses_same_decision_service(process):
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    from jshi.decision import decision_service
    model = Model([preparation()])
    gate = VoiceJEV(model, mode='decision', fallback_models=[])
    service = decision_service(process)
    service.config['mode'] = 'active'
    calls = []
    async def decide(purpose, state, **kwargs):
        calls.append(purpose)
        return decision()
    service.decide = decide
    async def scenario():
        async with TestClient(TestServer(create_app(process, 'stone', VoiceConfig(api_key=''), jev=gate))) as client:
            response = await client.post('/text-input', headers={
                'Origin': str(client.make_url('')).rstrip('/'), 'Accept': 'application/x-ndjson'},
                json={'text': '解释一下', 'object_id': 'lux-id'})
            assert response.status == 200
            await response.read()
            assert calls == ['entry']
            assert [r.purpose for r in model.requests if r.purpose != 'voice_jev_scene'] == ['voice_jev_prepare']
    asyncio.run(scenario())


def test_legacy_auth_failure_cools_down_the_original_gate():
    class Denied:
        def generate(self, request):
            exc = RuntimeError('denied')
            exc.status = 401
            raise exc
    gate = VoiceJEV(Denied(), mode='legacy', fallback_models=[])
    snapshot = copy(gate)
    snapshot._cooldown_owner = gate
    snapshot.decide_batch('stone', [{'n': 1, 'text': '你好', 'who': 'P1'}], [], {'state': 'completed'})
    assert gate._unavailable_until > monotonic()
    assert '401' in gate._unavailable_error
    assert snapshot._unavailable_until == 0


@pytest.mark.parametrize('expression', ['incomplete', 'uncertain'])
def test_background_unfinished_scope_uses_full_entry(expression):
    result = decision(address='other', expression=expression, effect='keep')
    assert entry_action(result, ['I1'], defaults()['entry_thresholds']['bailian']) is None
