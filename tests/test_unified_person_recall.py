from dataclasses import replace

from jshi.memory.port import RecalledFragment
from jshi.memory.shell import MemoryShell
from jshi.recognition.acquaintance import AcquaintanceStore
from jshi.voice.identity_comparison import compare_identity, voice_confidence_marker
from tests.test_voice import process


def test_many_links_use_one_pool_preserve_provenance_and_revoke(tmp_path):
    links = AcquaintanceStore(tmp_path / 'people.db')
    keys = [links.link('s', f'old{i}', 'lux', [f'input{i}'], basis='manual_annotation') for i in range(17)]
    links.link('s', 'old0', 'lux', ['another-input'], basis='manual_annotation')
    rows = [RecalledFragment(f'm{i}', 'fact', f'旧记忆{i}', object_id=f'old{i}',
                            source_ids=(f'input{i}',), score=1-i/100,
                            interlocutor=f'old{i}', interlocutor_object_ids=(f'old{i}',)) for i in range(17)]
    mixed = replace(rows[0], event_id='mixed', source_ids=('input0', 'unconfirmed'))
    class Backend:
        def __init__(self): self.calls=[]
        def recall(self, *args, **kw):
            self.calls.append(kw)
            allowed={kw.get('object_id'), *kw.get('object_ids', ())}
            return tuple(r for r in [mixed, *rows, rows[0]] if r.object_id in allowed)[:kw['limit']]
    backend=Backend(); shell=MemoryShell(backend); shell.person_links=links
    got=shell.recall('s', '你还记得别的吗', object_id='lux', interlocutor_object_id='lux', limit=12)
    assert len(backend.calls)==1 and len(backend.calls[0]['object_ids'])==18
    assert len(got)==12 and all(r.object_id=='lux' for r in got)
    assert got[0].event_id=='m0' and got[0].source_ids==('input0',)
    assert got[0].origin_object_id=='old0' and got[0].identity_basis=='provenance_link'
    assert got[0].interlocutor=='lux' and got[0].interlocutor_object_ids==('lux',)
    assert rows[0].object_id=='old0'  # No destructive rewriting.
    uncapped=shell.recall('s','q',object_id='lux')
    assert len(uncapped)==17 and backend.calls[-1]['limit'] is None
    links.revoke(keys[0])
    again=shell.recall('s', '你还记得别的吗', object_id='lux', limit=12)
    assert len(backend.calls)==3 and 'm0' not in {r.event_id for r in again}
    original=shell.recall('s', '你还记得别的吗', object_id='old0', limit=12)
    assert any(r.event_id=='m0' and r.object_id=='old0' for r in original)


def test_no_link_keeps_backend_arguments_and_zero_limit(tmp_path):
    class Backend:
        def recall(self, *args, **kw):
            self.kw=kw
            return ()
    backend=Backend(); shell=MemoryShell(backend)
    shell.person_links=AcquaintanceStore(tmp_path/'people.db')
    shell.recall('s','q',object_id='lux',limit=0,anchor_event_ids=('anchor',))
    assert backend.kw['object_ids']==() and backend.kw['limit']==0
    assert backend.kw['anchor_event_ids']==('anchor',)


def test_voice_marker_retains_raw_scores_without_changing_identity():
    speaker={'object_id':'lux','status':'recognized','method':'score_confirmation'}
    voices=[{'object_id':'lux','score':.2901234,'source':'repeated_short_audio'}]
    classifier={'ranking':[{'object_id':'lux','score':.9998765}]}
    marker=voice_confidence_marker(speaker,voices,classifier,compare_identity(voices,classifier))
    assert marker['voice_similarity'][0]['score']==.2901234
    assert marker['mlp_scores'][0]['score']==.9998765
    assert marker['score_confirmation']['object_id']=='lux'
    assert marker['attributed_object_id']=='lux'
    assert speaker=={'object_id':'lux','status':'recognized','method':'score_confirmation'}
    marker['voice_similarity'][0]['score']=0
    assert voices[0]['score']==.2901234


def test_revocation_during_retrieval_is_checked_before_return(tmp_path):
    links=AcquaintanceStore(tmp_path/'people.db')
    key=links.link('s','old','lux',['i'],basis='manual_annotation')
    class Backend:
        def recall(self,*args,**kwargs):
            links.revoke(key)
            return (RecalledFragment('m','fact','old',object_id='old',source_ids=('i',)),)
    shell=MemoryShell(Backend());shell.person_links=links
    assert not shell.recall('s','q',object_id='lux',limit=12)


def test_link_change_clears_only_related_cache_and_rejects_late_commit(tmp_path):
    from jshi.memory.working_set import RecallWorkingSetStore
    from jshi.reflection.memory_store import ReflectionMemoryStore
    from jshi.memory.contracts import RecallRequestContext
    store=RecallWorkingSetStore(ReflectionMemoryStore(tmp_path/'cache.db'))
    request=RecallRequestContext('s','a','session','记忆',object_ids=('lux',),interlocutor_object_id='lux')
    other=replace(request,object_ids=('qf',),interlocutor_object_id='qf')
    row=RecalledFragment('m','fact','记忆',object_id='lux',origin_object_id='old',source_ids=('i',))
    assert store.commit_visible(request,[row],expected_version=0)
    assert store.commit_visible(other,[replace(row,object_id='qf')],expected_version=0)
    version,_=store.load_scope(request,ttl=1800,similarity=0)
    store.invalidate_people('s',('old','lux'))
    current,rows=store.load_scope(request,ttl=1800,similarity=0)
    assert not rows and current>version
    assert not store.commit_visible(request,[row],expected_version=version)
    assert store.load_scope(other,ttl=1800,similarity=0)[1]
    assert store.commit_visible(request,[],expected_version=current)


def test_voice_evidence_is_persisted_separately_from_person(process):
    import asyncio
    from jshi.app.voice import VoiceConversation
    from jshi.core.envelope import SceneUtterance, SpeakerEvidence
    from jshi.subject.domain import HistoryKind
    from tests.test_voice import FakeCloud
    async def run():
        async def send(message): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            who=SpeakerEvidence('A','lux-id','lux','recognized','score_confirmation')
            c.candidate_cache['i']=[{'object_id':'lux-id','label':'lux','score':.2901234,'source':'voiceprint'}]
            c.input_records['i']={'personal_classifier':{'ranking':[{'object_id':'lux-id','score':.9998765}]}}
            c._capture_jev_inputs([SceneUtterance('匠石你好',who,0,1000,input_id='i')])
            records=process.repository.list_history('stone',HistoryKind.FACT)
            row=next(r.content for r in records if r.event_type=='voice_scene_input')
            assert row['speaker']['object_id']=='lux-id'
            assert row['voice_confidence']['voice_similarity'][0]['score']==.2901234
            assert row['voice_confidence']['mlp_scores'][0]['score']==.9998765
            from types import SimpleNamespace
            archived={}
            def update(iid,**values): archived.update(values)
            previous=c.sample_source
            try:
                c.sample_source=SimpleNamespace(archive=SimpleNamespace(update=update))
                c.source_speakers['i']=who
                c.input_records['i']['personal_classifier']['ranking'][0]['score']=.98
                await c._archive_update('i',classifier_result={'status':'completed'})
                assert archived['voice_confidence']['mlp_scores'][0]['score']==.98
                assert archived['voice_confidence']['attributed_object_id']=='lux-id'
            finally: c.sample_source=previous
        finally: await c.close()
    asyncio.run(run())
