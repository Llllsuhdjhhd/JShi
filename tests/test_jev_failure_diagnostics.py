"""Failure isolation, field validation and explicit-text fallback."""
import asyncio
import json
from urllib.error import URLError

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jshi.models.routing import ProviderFailure
from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV, BatchDecision
from jshi.voice.jev_errors import failure_details, JEVStaleError, JEVCooldownError
from jshi.voice.jev_scene import JEVSceneStore, JEVSceneWriter
from tests.test_jev_split_modes import Model, entry, preparation
from tests.test_voice import process


@pytest.mark.parametrize('exc,phase,expected', [
    (ProviderFailure(402, False), 'model_call', 'billing'),
    (ProviderFailure(403, False), 'model_call', 'authentication'),
    (ProviderFailure(429, True), 'model_call', 'rate_limit'),
    (ProviderFailure(500, True), 'model_call', 'provider_http'),
    (URLError('closed connection'), 'model_call', 'transport'),
    (TimeoutError(), 'model_call', 'timeout'),
    (JEVStaleError(), 'output_schema', 'stale'),
    (JEVCooldownError(), 'internal', 'cooldown'),
    (URLError(TimeoutError()), 'model_call', 'timeout'),
    (ValueError('length 402 invalid'), 'output_schema', 'output_schema'),
])
def test_failure_category_uses_operation_and_http_status(exc, phase, expected):
    assert failure_details(exc, phase)['error_kind'] == expected


def test_schema_error_reports_missing_and_extra_fields():
    from jshi.voice.jev_serial import validate, preparation_schema
    with pytest.raises(ValueError, match=r'missing=.*route.*extra=.*oops'):
        validate({'oops': 1}, preparation_schema())


def test_billing_failure_cools_down_without_a_second_model_call():
    model = Model([ProviderFailure(402, False)])
    gate = VoiceJEV(model, mode='split', fallback_models=[])
    batch = [{'n': 1, 'who': 'P1', 'text': '?'}]
    first = gate.decide_batch('stone', batch, [], {})
    assert first.stage_records[-1]['error_kind'] == 'billing'
    second = gate.decide_batch('stone', batch, [], {})
    assert second.stage_records[-1]['error_kind'] == 'cooldown'
    assert len(model.requests) == 1


def test_second_stage_transport_failure_does_not_report_first_stage_raw():
    model = Model([entry(), URLError('closed')])
    result = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch(
        'stone', [{'n': 1, 'who': 'P1', 'text': 'hello'}], [], {})
    assert result.stage_records[-1]['error_kind'] == 'transport'
    assert result.stage_records[-1]['raw'] == ''
    assert result.stage_records[-1]['stage'] == 'voice_jev_prepare'


def test_empty_optional_history_list_is_accepted_without_history(tmp_path):
    class SceneModel:
        def generate(self, request):
            schema = json.loads(request.system_extra.split('JSON Schema：')[1])
            assert schema['properties']['discard_history']['maxItems'] == 0
            return ModelResponse(text='{"scene":"当前问候","discard_history":[]}', model='fake')
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('a', {'text':'hello'})
    snapshot = store.snapshot()
    scene, _ = JEVSceneWriter(SceneModel()).run('stone', snapshot, '')
    assert store.commit(snapshot, scene)
    assert not store.snapshot()['events']


def test_scene_billing_failure_preserves_cooldown_and_pending_events(tmp_path):
    model = Model([ProviderFailure(402, False)])
    # This fixture special-cases scene generation, so use a counted failing model.
    calls = []
    def fail(request):
        calls.append(request)
        raise ProviderFailure(402, False)
    model.generate = fail
    writer = JEVSceneWriter(model)
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('a', {'text':'hello'})
    snapshot = store.snapshot()
    with pytest.raises(ProviderFailure):
        writer.run('stone', snapshot, '')
    assert snapshot['failure']['error_kind'] == 'billing'
    with pytest.raises(JEVCooldownError):
        writer.run('stone', store.snapshot(), '')
    assert len(calls) == 1 and len(store.snapshot()['events']) == 1


@pytest.mark.parametrize('kind,control,fallback', [
    ('output_schema', 'keep', True), ('transport', 'keep', True),
    ('stale', 'keep', False), ('output_schema', 'stop', False),
])
def test_selected_text_failure_is_explicit_and_never_silently_ignored(process, kind, control, fallback):
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    class FailedGate(VoiceJEV):
        def decide_envelope(self, *args, **kwargs):
            return BatchDecision(action='ignore', route='background', control=control,
                model_error='fixture failure', stage_records=({'stage':'voice_jev_entry',
                'status':'failed', 'error_kind':kind},))
    async def run():
        app = create_app(process, 'stone', VoiceConfig(api_key=''), jev=FailedGate())
        async with TestClient(TestServer(app)) as client:
            origin = str(client.make_url('')).rstrip('/')
            response = await client.post('/text-input', headers={
                'Origin':origin, 'Accept':'application/x-ndjson'}, json={'text':'？','object_id':'lux-id'})
            events = [json.loads(line) for line in (await response.text()).splitlines()]
            if fallback:
                assert any(e['type']=='jev_fallback' and e['failure_kind']==kind for e in events)
                done = next(e for e in events if e['type']=='complete')
                assert done['route']=='main' and done['jev_fallback']
                assert process.last_model_response is not None
            else:
                assert events[-1]['type']=='error'
                assert process.last_model_response is None
            diagnostics = [r.content for r in process.repository.list_history('stone')
                           if r.event_type=='text_jev_diagnostics']
            assert diagnostics and diagnostics[-1]['failure_kind']==kind
            assert diagnostics[-1]['fallback_to_main'] == fallback
    asyncio.run(run())


def test_background_scene_schema_failure_keeps_raw_and_request_link(process):
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    class SceneModel:
        name = 'fixture-scene'
        def generate(self, request):
            return ModelResponse(text='{"scene":"当前现场","unexpected":[]}', model=self.name)
    class BackgroundGate(VoiceJEV):
        def decide_envelope(self, *args, **kwargs):
            return BatchDecision(action='ignore', route='background', control='keep', judged=True)
    async def run():
        gate = BackgroundGate(SceneModel(), mode='split')
        async with TestClient(TestServer(create_app(process, 'stone', VoiceConfig(api_key=''), jev=gate))) as client:
            origin = str(client.make_url('')).rstrip('/')
            response = await client.post('/text-input', headers={'Origin':origin},
                                         json={'text':'hello','object_id':'lux-id'})
            body = await response.json()
            assert body['route'] == 'control'
        rows = [r.content for r in process.repository.list_history('stone')
                if r.event_type=='text_jev_scene_failure']
        assert len(rows) == 1
        assert rows[0]['error_kind'] == 'output_schema'
        assert "extra=['unexpected']" in rows[0]['error']
        assert json.loads(rows[0]['model_raw'])['unexpected'] == []
        assert rows[0]['prompt_activity_id'].startswith('jev-scene-text-')
    asyncio.run(run())
