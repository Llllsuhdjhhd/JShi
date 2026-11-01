import pytest
from jshi.decision.jev_native import entry_questions, decode_entry, PREPARE
from jshi.decision.base import DecisionResult
from jshi.decision.service import defaults
from jshi.voice.jev import VoiceJEV
from tests.test_jev_split_modes import Model, entry, preparation
from tests.test_jev_decision_mode import native_answer


def run_native(prepare_failure=False, rows=1):
    model=Model([preparation()])
    questions=[]
    def bridge(state, **kw):
        q=kw['questions'];questions.append(q)
        result=native_answer(q)
        if prepare_failure and q is PREPARE: result=DecisionResult('timeout')
        return result, defaults()
    bridge.protocol='native-v3';bridge.report=lambda *args: None
    result=VoiceJEV(model,mode='decision',fallback_models=[]).decide_batch('s',
        [{'n':i,'who':'P1','text':'匠石，解释一下','candidates':[{'who':'P1'}]} for i in range(1,rows+1)],[],
        {'_decision_entry':bridge})
    return result,model,questions


def test_native_replaces_both_generated_judgments():
    result,model,questions=run_native()
    assert not result.model_error and result.route=='main'
    assert result.items[0].speaker_pick=='unknown'
    assert len(questions)==2 and not model.requests


def test_prepare_failure_keeps_native_identity_and_only_falls_back_prepare():
    result,model,questions=run_native(True)
    assert not result.model_error
    assert [r.purpose for r in model.requests]==['voice_jev_prepare']


def test_three_rows_fit_without_identity_generation():
    result,model,questions=run_native(rows=3)
    assert not result.model_error and len(result.items)==3
    assert len(questions[0])==14
    assert not model.requests


def test_four_rows_fall_back_before_native_request():
    result,model,questions=run_native(rows=4)
    assert not questions
    assert model.requests[0].purpose=='voice_jev_entry'


def test_background_does_not_require_complete_expression():
    from tests.test_decision_runtime import answers
    from jshi.voice.jev_serial import entry_schema
    lines=[{'i':1,'p':'unknown','evidence_ref':'I1'}]
    schema=entry_schema(lines,{'I1'});q=entry_questions(lines,schema)
    result=DecisionResult('success',answers(q,dict(p1='unknown',c1='unknown',a1='other',e1='incomplete',effect='keep',work='none')))
    decoded=decode_entry(result,lines,q,.9)
    assert decoded['route']=='background' and not decoded['pending_refs']


def test_native_short_reply_only_generates_text_after_permissions():
    from tests.test_decision_runtime import answers
    model=Model([{'reply':'你好。'}])
    def bridge(state, **kw):
        q=kw['questions']
        if q is PREPARE:
            values=dict(level='L3',memory='no',image='no',route='self_reply',ack='no')
        else:
            values=dict(p1='P1',c1='confirmed',a1='direct',e1='complete',effect='redirect',work='none')
        return DecisionResult('success',answers(q,values)),defaults()
    bridge.protocol='native-v3';bridge.report=lambda *a:None
    value=VoiceJEV(model,mode='decision',fallback_models=[]).decide_batch('s',
        [{'n':1,'who':'P1','text':'你好','source':'web_text','speaker':{'method':'user_selected'}}],[],
        {'_decision_entry':bridge,'short_chat':{'allowed':True,'person_code':'P1'}})
    assert not value.model_error and value.route=='self_reply' and value.direct_reply=='你好。'
    assert [r.purpose for r in model.requests]==['voice_jev_reply']
