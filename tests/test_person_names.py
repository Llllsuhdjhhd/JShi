import json
from types import SimpleNamespace
from jshi.core.person_names import PersonNames, known_names
from jshi.models import ModelRequest, ModelResponse
from jshi.core import SubjectState
from jshi.skill.cognition import CognitionSkill


def test_known_names_do_not_merge_same_name_or_temporary_objects():
    rows = [SimpleNamespace(object_id=oid, label=label, status='confirmed', source='manual')
            for oid, label in [('a','lux'), ('b','小林'), ('c','小林'), ('d','待定声音 1')]]
    profiles = SimpleNamespace(get=lambda oid: next((p for p in rows if p.object_id == oid), None), list=lambda: rows)
    assert known_names({'P3':'a','P4':'b','P5':'c','P6':'d'}, profiles) == {'P3': 'lux'}


def test_projection_roundtrip_preserves_original_utterances():
    names = PersonNames({'P3': 'lux'})
    raw = '【当前输入】\n' + json.dumps([{'p':'P3','text':'为什么叫我P3？','evidence_ref':'I1'}],ensure_ascii=False)
    text = names.material(raw)
    assert '"p":"lux"' in text and '为什么叫我P3？' in text
    assert names.prose('P3（lux）说“P3是个什么名字”') == 'lux说“P3是个什么名字”'
    assert names.prose('B8[1分钟前] P3（lux）：为什么叫我P3？') == 'B8[1分钟前] lux：为什么叫我P3？'
    schema = {'properties': {'person': {'enum': ['P3','P4','unknown']}}}
    assert names.schema(schema)['properties']['person']['enum'] == ['lux','P4','unknown']
    assert names.value({'items':[{'person':'lux'}], 'reply':'我知道P3'}, reverse=True) == {'items':[{'person':'P3'}], 'reply':'我知道P3'}


def test_main_named_reply_targets_return_to_internal_codes():
    class Model:
        name = 'fake'
        def generate(self, req):
            return ModelResponse(model='fake', text=json.dumps({'mode':'respond','reply':'你好，lux',
                'action':'无动作','reason':'','unsaid':'','reply_targets':['lux']}))
    req = ModelRequest('subject_activity', '你好', SubjectState('stone','匠石','测试'),
                       persona_schema={'type':'object'}, person_name_codes=(('lux','P3'),))
    result = CognitionSkill(Model()).run(req)
    assert result.response_plan.items[0].target_ids == ('P3',)
    assert 'lux' in result.raw_text


def test_serial_entry_and_preparation_use_names_and_restore_identity():
    from tests.test_jev_split_modes import run, entry, preparation
    first = entry()
    first['items'][0]['person'] = 'lux'
    result, model = run('split', first, preparation(), delivery={
        'state':'completed', 'named_people': {'P1':'lux'},
        'people': {'P1':'lux'}, 'jev_scene':'P1（lux）在说话。'})
    assert result.judged and result.items[0].speaker_pick == 'P1'
    assert '"p":"lux"' in model.requests[0].input_text
    assert '"person":"lux"' in model.requests[1].input_text


def test_native_decision_uses_existing_name_and_restores_code():
    from time import time
    from jshi.voice.jev import VoiceJEV
    from jshi.decision.base import DecisionResult
    from jshi.decision.service import defaults
    from tests.test_decision_runtime import answers
    from tests.test_jev_split_modes import Model, preparation
    seen = []
    def bridge(state, **kwargs):
        questions = kwargs['questions']
        seen.append((state, questions))
        values = {}
        choices = {'p1':'lux','c1':'confirmed','a1':'direct','e1':'complete',
                   'effect':'redirect','work':'none','level':'L2','memory':'no','image':'no','route':'main','ack':'no'}
        for key in questions:
            values[key] = choices[key]
        return DecisionResult('success', answers(questions, values), model='native'), defaults()
    bridge.protocol = 'native-v3'
    bridge.report = lambda *args: None
    gate = VoiceJEV(Model([preparation()]), mode='decision', fallback_models=[])
    result = gate.decide_batch('s', [{'n':1,'who':'P1','text':'匠石你好','at':time()*1000,
                                    'candidates':[{'who':'P1'}]}], [],
                               {'state':'completed','named_people':{'P1':'lux'},'_decision_entry':bridge})
    assert result.judged and result.items[0].speaker_pick == 'P1'
    assert 'lux' in seen[0][1]['p1']['criteria']
    assert '"p":"lux"' in seen[0][0]['material']
