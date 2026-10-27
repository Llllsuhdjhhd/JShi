"""Check actual outgoing HTTP bodies rather than prompt-only JSON instructions."""
from io import BytesIO
import json
import pytest

from jshi.core import SubjectState
from jshi.models.base import ModelRequest, _chat_payload
from jshi.models.routing import ProviderModel, _TimeoutBound
from jshi.voice.jev_serial import preparation_schema, validate
from dataclasses import replace


def request(purpose):
    return ModelRequest(purpose=purpose, input_text='你好',
        subject_state=SubjectState('stone', '匠石', purpose), system_extra='只输出JSON对象')


@pytest.mark.parametrize('purpose', ['voice_jev', 'voice_jev_entry', 'voice_jev_prepare', 'voice_jev_scene'])
@pytest.mark.parametrize('adapter', ['base', 'routed', 'capped', 'stream'])
def test_all_deepseek_jev_paths_enable_json_mode(purpose, adapter, monkeypatch):
    incoming = request(purpose)
    if adapter == 'base':
        body = json.loads(_chat_payload('deepseek-v4-flash', incoming))
    else:
        provider = ProviderModel({'protocol': 'chat', 'label': 'D', 'endpoint': 'https://example.test/v1',
            'api_key': 'fixture'}, 'deepseek-v4-flash')
        captured = []
        def open_response(self, http):
            captured.append(json.loads(http.data))
            if adapter == 'stream':
                return BytesIO(b'data: {"choices":[{"delta":{"content":"{}"}}]}\n\ndata: [DONE]\n')
            return BytesIO(b'{"choices":[{"message":{"content":"{}"},"finish_reason":"stop"}]}')
        monkeypatch.setattr(ProviderModel, '_open', open_response)
        if adapter == 'stream':
            assert ''.join(provider.generate_stream(incoming)) == '{}'
        else:
            model = _TimeoutBound(provider, 2) if adapter == 'capped' else provider
            result = model.generate(incoming)
            assert result.metadata['finish_reason'] == 'stop'
            assert result.metadata['response_format'] == 'json_object'
        body = captured[0]
    assert body['response_format'] == {'type': 'json_object'}
    assert body['max_tokens'] == 8192


def test_other_requests_and_providers_keep_existing_format():
    assert 'response_format' not in json.loads(_chat_payload('deepseek-v4-flash', request('cognition')))
    assert 'response_format' not in json.loads(_chat_payload('qwen3.5-flash', request('voice_jev')))
    body = json.loads(_chat_payload('deepseek-v4-flash', request('voice_jev'), max_tokens=2048))
    assert body['max_tokens'] == 2048


def test_valid_json_still_rejects_wrong_field_type():
    with pytest.raises(ValueError):
        validate({'route': 'main', 'level': '2', 'recall_memory': False, 'main_image': 'omit',
                  'reply': '', 'waiting_reply': '', 'reason': '需要理解'}, preparation_schema())


@pytest.mark.parametrize('purpose', ['subject_activity', 'write_zone', 'introspection', 'tool_internal', 'memory_evaluation'])
def test_explicit_contract_covers_all_structured_skills(purpose):
    incoming = replace(request(purpose), json_output=True)
    body = json.loads(_chat_payload('deepseek-v4-flash', incoming))
    assert body['response_format'] == {'type': 'json_object'}
    assert 'max_tokens' not in body  # Keep the existing thinking/output budget.


@pytest.mark.parametrize('kind', ['base', 'cognition', 'zone', 'wrap'])
@pytest.mark.parametrize('stream', [False, True])
def test_skill_overrides_retain_explicit_contract(kind, stream):
    from jshi.skill import Skill, CognitionSkill, WriteZoneSkill
    from jshi.skill.tool_wrap import ToolWrapSkill
    from jshi.models import ModelResponse
    class Tiny(Skill):
        def parse(self, data): return data
    class Capture:
        name = 'deepseek-v4-flash'
        requests = []
        def generate(self, incoming):
            self.requests.append(incoming)
            return ModelResponse(text='{}', model=self.name)
        def generate_stream(self, incoming):
            self.requests.append(incoming)
            yield '{}'
    model = Capture()
    model.requests = []
    skill = {'base': Tiny, 'cognition': CognitionSkill, 'zone': WriteZoneSkill, 'wrap': ToolWrapSkill}[kind](model)
    incoming = request('subject_activity')
    if kind == 'cognition' and stream:
        incoming = replace(incoming, persona_schema={'type': 'object'})
    if kind == 'wrap' and not stream:
        skill._run_with_instruction(incoming, '只输出JSON')
    else:
        (skill.run_stream if stream else skill.run)(incoming)
    assert model.requests[0].json_output is True
    body = json.loads(_chat_payload(model.name, model.requests[0]))
    assert body['response_format'] == {'type': 'json_object'}


def test_chat_routing_preserves_structured_visual_mode():
    from jshi.models.routing import RoutedModel
    from jshi.models import ModelResponse
    class Capture:
        name = 'deepseek-v4-flash'
        def chat(self, messages, max_tokens=None, **options):
            assert options == {'response_format': 'json_object'}
            return ModelResponse(text='{}', model=self.name)
    RoutedModel(Capture()).chat([{'role': 'user', 'content': 'JSON'}], response_format='json_object')


def test_visual_settings_bridge_keeps_format_for_original_transport(tmp_path):
    from types import SimpleNamespace
    from tests.test_model_settings import process
    from jshi.app.model_settings import ModelSettings
    calls = []
    def transport(body):
        calls.append(body)
        return {'choices': [{'message': {'content': '{}'}}]}
    runtime = process(tmp_path)
    runtime.vision = SimpleNamespace(config=SimpleNamespace(model='deepseek-v4-flash'),
                                    model=SimpleNamespace(transport=transport))
    ModelSettings(tmp_path, runtime, None)
    runtime.vision.model.transport({'messages': [{'role': 'user', 'content': 'JSON'}],
                                    'response_format': {'type': 'json_object'}})
    assert calls[0]['response_format'] == {'type': 'json_object'}
