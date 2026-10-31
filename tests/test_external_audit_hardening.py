import json
from dataclasses import replace

import pytest

from jshi.models import ModelResponse
from jshi.models.base import ToolUseIntent
from jshi.skill.cognition import CognitionSkill
from jshi.voice.jev_scene import JEVSceneStore
from tests.test_skill import FakeModel, make_request
from tests.test_voice import process


@pytest.mark.parametrize('bad', ['invalid', {'invalid': 1}, ['invalid'], float('inf')])
@pytest.mark.parametrize('field', ['budget', 'level', 'importance'])
def test_bad_optional_number_preserves_reply_and_valid_siblings(bad, field):
    recall = {'query': '有效回忆', 'budget': 2, 'level': 3}
    rank = {'id': 'M1', 'importance': .7}
    data = {
        'response_plan': {'mode': 'respond', 'items': [{'channel': 'verbal', 'text': '完整回答'}]},
        'recall_requests': [{**recall, 'query': '坏条目', field: bad}, recall]
            if field != 'importance' else [recall],
        'importance_ranking': [{**rank, 'id': 'bad', field: bad}, rank]
            if field == 'importance' else [rank],
    }
    result = CognitionSkill(FakeModel(data)).run(make_request())
    assert result.text == '完整回答'
    assert [r.query for r in result.recall_requests] == ['有效回忆']
    assert [r.id for r in result.importance_ranking] == ['M1']


@pytest.mark.parametrize('bad', [7, 'invalid', {'unexpected': 1}])
def test_optional_sections_must_be_arrays(bad):
    result = CognitionSkill(FakeModel({
        'response_plan': {'mode': 'respond', 'items': [{'channel': 'verbal', 'text': '回答'}]},
        'recall_requests': bad, 'importance_ranking': bad,
    })).run(make_request())
    assert result.text == '回答'
    assert result.recall_requests == result.importance_ranking == ()


@pytest.mark.parametrize('reply_first', [True, False])
@pytest.mark.parametrize('mode', ['respond', 'think', 'wait', 'ignore', None])
def test_persona_stream_emits_once_as_soon_as_both_fields_arrive(reply_first, mode):
    calls = []
    class Model:
        name = 'stream'
        def generate_stream(self, request):
            fields = [('reply', '你好'), ('mode', mode)]
            if not reply_first:
                fields.reverse()
            yield '{' + json.dumps(fields[0][0]) + ':' + json.dumps(fields[0][1]) + ','
            assert calls == []
            yield json.dumps(fields[1][0]) + ':' + json.dumps(fields[1][1]) + ','
            assert calls == (['你好'] if mode == 'respond' else [])
            yield '"reason":"test","action":""}'
    request = replace(make_request(), persona_schema={'type': 'object'})
    result = CognitionSkill(Model()).run_stream(request, on_reply=calls.append)
    assert calls == (['你好'] if mode == 'respond' else [])
    assert result.text == ('你好' if mode == 'respond' else '')


@pytest.mark.parametrize('persona', ['wood', 'smith'])
@pytest.mark.parametrize('level', [1, 2, 3])
def test_tool_permission_follows_profile_for_both_personas(process, persona, level):
    process.style_packs.set('stone', persona)
    current = replace(process.assemble_current_state('stone', '你好'), prompt_level=level)
    class Model:
        def generate(self, request):
            return ModelResponse(model='test', text='回答', tool_intent=ToolUseIntent('测试需求'),
                tool_consumed=('task-1',), tool_handling=({'task_id': 'task-1', 'disposition': 'answered'},))
    process.cognition = Model()
    result = process._cognize_once(current, (), activity_id='audit-tools')
    assert result.text == '回答'
    assert bool(result.tool_intent) == (level == 1)
    assert bool(result.tool_consumed) == (level == 1)
    assert bool(result.tool_handling) == (level == 1)


def test_background_scene_commit_keeps_cutoff_evidence_valid(tmp_path, monkeypatch):
    import jshi.voice.jev_scene as scene_module
    monkeypatch.setattr(scene_module, 'time', lambda: 1)
    store = JEVSceneStore(tmp_path / 'scene.json')
    store.append('a', {'text': '原话', 'at': 1000})
    before = store.material_view(1500)
    monkeypatch.setattr(scene_module, 'time', lambda: 2)
    assert store.commit(store.snapshot(), '后台整理后的片场')
    after = store.material_view(1500)
    # The entrance deliberately reads at its input cutoff, not latest wall time.
    assert before == after
    assert after['version'] != store.state['version']
    store.append('a', {'text': '纠正后的原话', 'at': 1000})
    assert store.state['generation'] != before['generation']
