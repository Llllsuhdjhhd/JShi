import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jshi.core import SubjectState
from jshi.core.envelope import SceneUtterance, SpeakerEvidence
from jshi.models import ModelRequest, ModelResponse
from jshi.subject.preparation import PreparedAnalysis, NeedDeep, prepare
from jshi.skill.cognition import CognitionSkill
from jshi.voice.feedback import FeedbackArbiter
from tests.test_voice import process, envelope


def row(text='前段', n=0):
    return SceneUtterance(text,SpeakerEvidence('A','person','甲','recognized'),n*1000,(n+1)*1000,input_id=str(n))


def test_main_cognition_material_does_not_reuse_preparation_memory_codes():
    from types import SimpleNamespace
    from datetime import datetime, timezone
    memory = SimpleNamespace(content='旧事', text='', occurred_at=datetime(2026, 10, 28, tzinfo=timezone.utc), event_id='long-id')
    analysis = PreparedAnalysis('s', 'person', (row(),), 2, {'goal': '暂定'}, memory_rows=(memory,))
    assert analysis.model_material()['memories'][0]['ref'] == 'R1'
    spoken = analysis.model_material(include_memories=False)
    assert set(spoken) == {'prefix', 'preparation'}


def test_provisional_analysis_has_no_business_commit_and_no_long_ids():
    seen=[]
    class Model:
        name='fixture'
        def generate(self,request):
            seen.append(request)
            return ModelResponse(text=json.dumps({'goal':'介绍经历','assumptions':[],'gaps':['长度未定'],'candidate':'候选介绍'}),model='fixture')
    process=SimpleNamespace(memory=object(),cognition=Model(),_record_step_input=lambda *a,**k:None)
    result=prepare(process,'subject','', (row(),),2,lambda:True)
    assert result.content['candidate']=='候选介绍'
    assert 'person' not in seen[0].input_text and 'subject' not in seen[0].input_text
    assert seen[0].purpose=='voice_preparation'


@pytest.mark.parametrize('change',['person','subject','scene','revision','missing'])
def test_preparation_scope_and_source_revision(change):
    original=row();analysis=PreparedAnalysis('s','person',(original,),2,{},created=__import__('time').monotonic())
    person='other' if change=='person' else 'person'
    subject='other' if change=='subject' else 's'
    version=3 if change=='scene' else 2
    rows=() if change=='missing' else (replace(original,text='新原话') if change=='revision' else original,)
    assert not analysis.compatible(subject,person,rows,version)


def test_light_generation_can_escalate_before_any_reply():
    class Raw:
        name='raw'
        def generate(self,req):
            assert 'need_deep' in req.system_extra and 'anyOf' in req.system_extra
            return ModelResponse(text='{"need_deep":true}',model='fixture')
    skill=CognitionSkill(Raw())
    request=ModelRequest('subject_activity','完整原话',SubjectState('s','匠石',''),light_finalize=True,
                         preparation_context='旧候选')
    spoken=[]
    with pytest.raises(NeedDeep):skill.run_stream(request,on_reply=spoken.append)
    assert not spoken


def test_light_invalid_json_escalates_instead_of_speaking_raw_text():
    class Raw:
        name='raw'
        def generate(self,req):return ModelResponse(text='半截错误答案',model='fixture')
    request=ModelRequest('subject_activity','完整原话',SubjectState('s','匠石',''),light_finalize=True)
    with pytest.raises(NeedDeep):CognitionSkill(Raw()).run(request)


def test_real_process_escalates_once_and_commits_only_final_input(process):
    class Model:
        def __init__(self):self.requests=[]
        def generate(self,request):
            self.requests.append(request)
            if request.light_finalize:raise NeedDeep()
            return ModelResponse(text='按完整输入重新分析后的回答',model='fixture')
    model=Model();process.cognition=model
    incoming,identities=envelope(process,'不是我面试，是我要面试别人')
    from jshi.subject import HistoryKind
    before=len([h for h in process.repository.list_history('stone',HistoryKind.FACT,100) if h.event_type=='external_input'])
    process.experience('stone',incoming.text,envelope=incoming,resolved_speaker=identities.candidate(incoming.speaker),
                        preparation_context='旧目标：我去面试',light_finalize=True)
    assert [r.light_finalize for r in model.requests]==[True,False]
    assert not model.requests[-1].preparation_context
    after=len([h for h in process.repository.list_history('stone',HistoryKind.FACT,100) if h.event_type=='external_input'])
    assert after-before==1


def test_feedback_shared_cooldown_and_stale_permission():
    now=[0];arbiter=FeedbackArbiter(lambda:now[0])
    assert not arbiter.claim('listening_ack',0,enabled=True)
    assert arbiter.claim('waiting_ack',0,enabled=True)
    assert not arbiter.claim('reconsider_ack',0,enabled=True)
    now[0]=31;arbiter.changed()
    assert not arbiter.claim('reconsider_ack',0,enabled=True)
    assert not arbiter.claim('reconsider_ack',1,speaking=True,enabled=True)
    assert arbiter.claim('reconsider_ack',1,enabled=True)


def test_rems_preparation_bypasses_facade_and_disables_reinforcement():
    from jshi.memory.rems3 import Rems3MemoryBackend
    calls=[]
    def recall(*args,**kwargs):calls.append(kwargs);return ()
    backend=Rems3MemoryBackend.__new__(Rems3MemoryBackend)
    backend._pipeline=SimpleNamespace(recall_pipeline=SimpleNamespace(recall=recall))
    assert backend.prepare_recall('s','原话','p')==((),{})
    assert calls==[{'object_ids':('p',),'interlocutor_object_id':'p','limit':6,'reinforce':False}]


def test_disabling_preparation_drops_inflight_or_cached_candidate():
    from jshi.voice.provisional import PreparationManager
    async def scenario():
        voice=SimpleNamespace(decisions=SimpleNamespace(config={'mode':'off','preparation':True}))
        manager=PreparationManager(voice)
        manager.analysis=PreparedAnalysis('s','p',(row(),),0,{})
        assert await manager.finish(None)==(None,'normal')
        assert manager.analysis is None and manager.cancelled.is_set()
    asyncio.run(scenario())


def test_generic_transport_light_override_preserves_config():
    from jshi.models.base import _chat_payload
    request=ModelRequest('subject_activity','完整原话',SubjectState('s','匠石',''),light_finalize=True)
    light=json.loads(_chat_payload('deepseek-v4-flash',request,thinking='enabled',reasoning_effort='high'))
    normal=json.loads(_chat_payload('deepseek-v4-flash',replace(request,light_finalize=False),thinking='enabled',reasoning_effort='high'))
    assert light['thinking']['type']=='disabled'
    assert normal['thinking']['type']=='enabled'


def test_light_schema_accepts_formal_response_and_rejects_empty_json():
    from jshi.models.schema_check import validate
    schema={'type':'object','properties':{'mode':{'enum':['respond']},'score':{'type':'number'}},'required':['mode']}
    validate({'mode':'respond','score':.5},schema)
    with pytest.raises(ValueError):validate({},schema)


def test_slow_read_does_not_hold_preparation_executor_after_cancellation():
    from threading import Event, Thread
    from jshi.subject.cancellation import TurnCancelled
    allowed,started,release,finished=Event(),Event(),Event(),Event()
    allowed.set();outcomes=[]
    def slow_read(*args):started.set();release.wait(2);return (),{}
    process=SimpleNamespace(memory=SimpleNamespace(prepare_recall=slow_read))
    def run():
        try:prepare(process,'s','p',(row(),),0,allowed.is_set)
        except TurnCancelled:outcomes.append('cancelled')
        finally:finished.set()
    task=Thread(target=run)
    task.start()
    try:
        assert started.wait(1)
        allowed.clear()
        assert finished.wait(1) and outcomes==['cancelled']
        assert not release.is_set()
    finally:
        release.set();task.join(2)


def test_cancelled_preparation_child_does_not_cancel_formal_turn():
    from jshi.voice.provisional import PreparationManager
    async def scenario():
        voice=SimpleNamespace(decisions=SimpleNamespace(config={'mode':'active','preparation':True}))
        manager=PreparationManager(voice)
        manager.task=asyncio.create_task(asyncio.Event().wait())
        manager.task.cancel()
        assert await manager.finish(None)==(None,'normal')
        assert not asyncio.current_task().cancelling()
    asyncio.run(scenario())
