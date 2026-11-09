from dataclasses import dataclass
from types import SimpleNamespace
import pytest
from jshi.recognition.acquaintance import AcquaintanceStore
from jshi.memory.shell import MemoryShell
from jshi.memory.port import RecalledFragment
from jshi.voice.personal_store import PersonalStore
from jshi.tool.hang import HangStore


def test_memory_recall_links_only_wholly_scoped_evidence_and_reverts(tmp_path):
    links=AcquaintanceStore(tmp_path/'people.db')
    key=links.link('s','a','lux',['i1'],basis='manual_annotation')
    class Backend:
        def recall(self,*args,**kwargs):
            if kwargs.get('object_id')=='a':
                return (RecalledFragment('m1','fact','初次问候',object_id='a',source_ids=('i1',)),
                    RecalledFragment('m2','fact','混合经历',object_id='a',source_ids=('i1','i2')))
            return ()
    shell=MemoryShell(Backend());shell.person_links=links
    rows=shell.recall('s','问候',object_id='lux',limit=6)
    assert [(r.event_id,r.object_id) for r in rows]==[('m1','lux')]
    links.revoke(key)
    assert not shell.recall('s','问候',object_id='lux',limit=6)


def test_samples_relabel_invalidates_models_and_revoke_restores_source(tmp_path):
    store=PersonalStore(tmp_path/'samples')
    sid=store.save(bytes(8000),'a','session','i1',source='manual')
    store.save(bytes(8000),'a','session','i2',source='manual')
    before=store.revision()
    store.link_inputs('link',['i1'],'session','lux',active=True)
    assert store.sample(sid)['object_id']=='lux'
    assert store.revision()[1]>before[1]
    assert store.sample(store.sample_id('session','i2'))['object_id']=='a'
    version=store.revision()
    store.link_inputs('link',['i1'],'session','lux',active=True)
    assert store.revision()==version
    store.link_inputs('link',['i1'],'session','lux',active=False)
    assert store.sample(sid)['object_id']=='a'


def test_task_owner_is_projected_without_rewriting_task_record(tmp_path):
    links=AcquaintanceStore(tmp_path/'people.db')
    tasks=HangStore(tmp_path/'hang.jsonl');tasks.person_links=links
    task=tasks.create(subject_id='s',object_id='a',need='查天气',command='search')
    # Existing tasks without provenance are not guessed into a new owner.
    key=links.link('s','a','lux',['i1'],basis='manual_annotation')
    assert tasks.get(task.task_id).object_id=='a'
    from dataclasses import replace
    tasks._records[task.task_id]=replace(task,field_ref={'input_id':'i1'})
    assert tasks.get(task.task_id).object_id=='lux'
    assert tasks.list_for('s','lux')[0].task_id==task.task_id
    assert tasks._records[task.task_id].object_id=='a'
    links.revoke(key)
    assert not tasks.list_for('s','lux') and tasks.get(task.task_id).object_id=='a'


def test_listing_projector_matches_single_projection(tmp_path):
    links=AcquaintanceStore(tmp_path/'people.db')
    assert links.projector('s')('a',['i1'])=='a'
    links.provenance('s','m1',['i1'])
    links.link('s','a','lux',['i1'],basis='manual_annotation')
    links.link('s','b','qf',['i3'],basis='manual_annotation')
    project=links.projector('s')
    for person,sources in [('a',['i1']),('a',['m1']),('a',['i1','i2']),('a',[]),('b',['i3']),('c',['i1'])]:
        assert project(person,sources)==links.project('s',person,sources)


def test_conflicting_person_link_requires_revocation(tmp_path):
    a=AcquaintanceStore(tmp_path/'people.db')
    k=a.link('s','a','lux',['i1'],basis='manual_annotation')
    with pytest.raises(ValueError): a.link('s','a','qf',['i1'],basis='manual_annotation')
    a.revoke(k)
    a.revoke(k)
    a.link('s','a','qf',['i1'],basis='manual_annotation')
    assert a.resolve('s','a','i1')=='qf'


def test_experience_projection_requires_complete_source_chain(tmp_path):
    from dataclasses import dataclass
    from jshi.longtermexperience.rems import RemsLongTermExperience
    @dataclass
    class Fragment:
        object_id:str
        content:str
        variant:str='full'
        source_event_ids:tuple=('m1',)
        updated_at:object=None
        score:float=1
    class Module:
        def recall(self,query):
            return SimpleNamespace(person=[Fragment('a','对方初次自报小陈')] if query.object_ids==('a',) else [])
    module=RemsLongTermExperience(Module(),lambda **kw:SimpleNamespace(**kw))
    links=AcquaintanceStore(tmp_path/'people.db');module.person_links=links
    links.provenance('s','m1',['i1'])
    key=links.link('s','a','lux',['i1'],basis='manual_annotation')
    assert module.recall_experience('s','称呼',object_ids=('lux',))[0].object_id=='lux'
    links.revoke(key)
    assert not module.recall_experience('s','称呼',object_ids=('lux',))


def test_replay_after_restart_and_revoke_restore_views(process):
    import asyncio
    from jshi.app.voice import VoiceConversation
    from jshi.core.envelope import SpeakerEvidence
    from jshi.voice.contracts import Transcript
    from jshi.recognition import ObjectProfile
    from jshi.recognition.link_updates import apply_link
    from tests.test_voice import FakeCloud
    process.profiles.create(ObjectProfile('a','临时人物A',source='voice_anonymous'))
    async def run():
        async def send(message): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            who=SpeakerEvidence('track','a','临时人物A','unknown','anonymous_voice_cluster')
            c.source_speakers['i1']=who
            c.input_records['i1']={'transcript':Transcript('你好','track',0,500,True),'pcm':None}
            c.unknown_inputs.append('i1','a','你好',c.session_id,0,500)
            c.dialogue.input('stone','i1','你好','临时人物A',actor_id='a')
            keys=c.link_selected_inputs(['i1'],'lux-id')
            key=keys[0]
            assert c.annotations['i1'].object_id=='lux-id'
            assert c.unknown_inputs.read('i1')['manual_object_id']=='lux-id'
            assert c.dialogue.recent('stone')[0]['who']=='lux'
            assert all(j['status']=='done' for j in process.acquaintance.jobs(key))
            c.revoke_person_link(key)
            assert c.annotations['i1'].object_id=='a'
            assert c.unknown_inputs.read('i1')['manual_object_id'] is None
            assert c.dialogue.recent('stone')[0]['who']=='临时人物A'
            assert all(j['status']=='done' for j in process.acquaintance.jobs(key))
            # Reopening and replaying a completed link is harmless.
            apply_link(process,key)
        finally: await c.close()
    asyncio.run(run())


from tests.test_voice import process


def test_voice_reference_withdrawal_preserves_other_inputs_and_people(process,tmp_path):
    from jshi.voice.speakers import LocalSpeakers
    bank=LocalSpeakers(None,'m',tmp_path/'bank.json',process.profiles)
    bank.known={'one':{'object_id':'lux-id','embedding':[1.,0.], 'references':[
        {'embedding':[1.,0.],'quality':1.,'session_id':'s','input_id':'i1'},
        {'embedding':[0.,1.],'quality':1.,'session_id':'s','input_id':'i2'}]},
        'other':{'object_id':'other','embedding':[1.,1.]}}
    bank.withdraw_reference_inputs('lux-id','s',['i1'])
    assert [r['input_id'] for r in bank.known['one']['references']]==['i2']
    assert 'other' in bank.known
    bank.withdraw_reference_inputs('lux-id','s',['i1'])
    assert 'one' in bank.known


def test_question_requires_delivery_and_refusal_is_preserved(tmp_path):
    state=AcquaintanceStore(tmp_path/'people.db')
    assert not state.context('s','a')['name_asked']
    state.reply_delivered('s','a','question1','怎么称呼你？',medium='audio')
    state.observe_text('s','a','answer1','不想说',session='session')
    assert state.context('s','a')['name_declined']
    assert not state.context('s','b')['name_asked']
    assert AcquaintanceStore(tmp_path/'people.db').context('s','a')['name_asked']


def test_typed_bare_name_requires_recent_delivered_question(process):
    from jshi.voice.identity import VoiceIdentities,typed_name_answer
    state=process.acquaintance
    ids=VoiceIdentities(process.profiles,'stone','s',process.repository)
    person=ids.resolve('A',cluster_id='one')
    assert not typed_name_answer(process,'stone',person,'小陈')
    state.reply_delivered('stone',person.object_id,'q','怎么称呼你？',medium='text')
    assert typed_name_answer(process,'stone',person,'小陈')=='小陈'
    assert not typed_name_answer(process,'stone',person,'不想说')
