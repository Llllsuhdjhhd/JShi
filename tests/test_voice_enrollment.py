from __future__ import annotations

import asyncio
import json
import numpy as np
import pytest

from jshi.app.voice import VoiceConversation
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.local import LocalSpeakers
from jshi.voice.volc import Transcript, TranscriptAssembler
from tests.test_voice import process, FakeCloud


def test_consistent_unmatched_voice_remains_a_pending_group(process, tmp_path):
    s = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles, threshold=.77)
    s.embedding = lambda a: [1., 0.]
    first = s.identify([0]*48000)[0]
    s.embedding = lambda a: [.7, (1-.7**2)**.5]
    assert s.identify([0]*48000)[0] == first
    assert first.startswith('pending-') and not s.tracks and not s.known
    assert s.pending_tracks[0]['count'] == 2


def test_distinct_voices_need_ten_observations_and_ambiguity_does_not_create_a_third(process, tmp_path):
    s = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles)
    s.embedding = lambda a: [1., 0.]
    first = s.identify([0]*48000)[0]
    s.embedding = lambda a: [0., 1.]
    second = s.identify([0]*48000)[0]
    assert second != first and not s.tracks
    for _ in range(9):
        assert s.identify([0]*48000)[0] == second
    assert len(s.tracks) == 1 and not s.last_match['tentative']
    s.embedding = lambda a: [1., 1.]
    assert s.identify([0]*48000)[0].startswith('unidentified-')
    assert len(s.pending_tracks) == 2


def test_cloud_recency_cannot_merge_dissimilar_pending_voices(process, tmp_path):
    s = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles)
    s.embedding = lambda a: [1., 0.]
    first = s.identify_timed([0]*48000, source_track='0', start_ms=0, end_ms=3000)[0]
    s.embedding = lambda a: [.43, (1-.43**2)**.5]
    other, vpid, _ = s.identify_timed([0]*32000, source_track='0', start_ms=3500, end_ms=5500)
    assert other != first and not vpid and s.last_match['tentative']
    assert not s.tracks and not s.known


def test_replayed_or_overlapping_audio_does_not_complete_collection(process, tmp_path):
    s = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles)
    s.embedding = lambda a: [1., 0.]
    first = s.identify_timed([0]*48000, start_ms=0, end_ms=3000)[0]
    for _ in range(12):
        assert s.identify_timed([0]*48000, start_ms=0, end_ms=3000)[0] == first
    s.identify_timed([0]*48000, start_ms=1000, end_ms=4000)
    assert s.pending_tracks[0]['count'] == 1 and s.last_match['tentative']
    s.reset_session()
    assert not s.pending_tracks and not s.tracks


def test_name_slider_does_not_change_pending_grouping(process, tmp_path):
    s = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles, threshold=.3)
    s.embedding = lambda a: [1., 0.]
    first = s.identify([0]*48000)[0]
    s.threshold = .95
    assert s.identify([0]*48000)[0] == first
    assert s.last_match['collection']['count'] == 2 and s.last_match['tentative']


def test_weak_named_match_does_not_create_a_person_or_update_registered_voice(process, tmp_path):
    from jshi.voice.identity import VoiceIdentities
    s = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles, threshold=.8)
    s.embedding = lambda a: [1., 0.]
    s.enroll_samples([bytes(96000)], 'lux-id', user_labeled=True)
    original = json.dumps(s.known, sort_keys=True)
    ids = VoiceIdentities(process.profiles, 'stone', 'session')
    audio = SessionAudio(); audio.append(bytes(96000))
    s.embedding = lambda a: [.43, (1-.43**2)**.5]
    count = len(process.profiles.list())
    t = audio.match(Transcript('test', '0', 0, 3000, True), s)
    assert t.identity_tentative and not t.voiceprint_id
    e = ids.resolve(t.track_id, cluster_id=t.speaker_cluster_id, tentative=t.identity_tentative)
    assert e.method == 'voice_continuity' and process.profiles.get(e.object_id) is None
    assert len(process.profiles.list()) == count and json.dumps(s.known, sort_keys=True) == original


def test_name_question_answer_then_text_rename_preserves_person_and_voiceprint(process, tmp_path):
    from jshi.voice.identity import VoiceIdentities
    ids=VoiceIdentities(process.profiles,'stone','session')
    visitor=ids.resolve('cloud-0',cluster_id='A')
    assert not ids.name_answer(visitor,'小明')
    ids.name_question_played(visitor.object_id,'你好，怎么称呼你？')
    other=ids.resolve('cloud-1',cluster_id='B')
    assert not ids.name_answer(other,'小明')
    assert not ids.name_answer(visitor,'不知道')
    assert not ids.name_answer(visitor,'我不想说')
    assert not ids.name_answer(visitor,'今天下雨')
    short=ids.resolve('cloud-0',uncertain=True)
    assert ids.name_answer(short,'小明。')=='小明'
    named,_=ids.introduce('cloud-0','小明',basis='name_answer',uncertain=True)
    assert named.object_id==visitor.object_id and not ids.name_questions
    s=LocalSpeakers(None,'test',tmp_path/'prints.json',process.profiles)
    s.embedding=lambda a:[1.,0.]
    s.enroll_samples([bytes(96000)],named.object_id,user_labeled=True)
    before=dict(s.known)
    process.profiles.rename(named.object_id,'小敏',expected_label='小明')
    track,vpid,_=s.identify([0]*48000)
    assert ids.resolve(track,voiceprint_id=vpid).label=='小敏'
    assert ids.resolve('cloud-9',cluster_id='A').object_id==visitor.object_id
    assert ids.associated(visitor).label=='小敏' and s.known==before
    with pytest.raises(ValueError,match='已变更'):
        process.profiles.rename(named.object_id,'另一个名',expected_label='小明')


def test_standalone_enrollment_and_text_correction_without_conversation(process, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    import base64
    s=LocalSpeakers(None,'test',tmp_path/'prints.json',process.profiles)
    s.embedding=lambda a:[1.,0.]
    async def run():
        client=TestClient(TestServer(create_app(process,'stone',VoiceConfig(api_key=''),online_speakers=s)))
        await client.start_server()
        try:
            origin=str(client.make_url('/')).rstrip('/')
            payload={'action':'enroll','name':'小明','clips':[base64.b64encode(bytes(320000)).decode()]}
            r=await client.post('/voice-registry',json=payload,headers={'Origin':origin})
            assert r.status==200,await r.text()
            data=await r.json();oid=data['object_id']
            assert data['seconds']==10 and len(s.known)==1
            r=await client.post('/voice-registry',json={'action':'rename','object_id':oid,'previous_name':'小明','name':'小敏'},headers={'Origin':origin})
            assert r.status==200 and (await r.json())['object_id']==oid
            assert process.profiles.get(oid).label=='小敏'
            assert next(iter(s.known.values()))['object_id']==oid
            r=await client.post('/voice-registry',json=payload,headers={'Origin':'https://other.example'})
            assert r.status==403
            r=await client.get('/voice-enroll');assert r.status==200
        finally:await client.close()
    asyncio.run(run())


def test_try_it_reports_recognition_and_recommends_threshold_without_learning(process, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    import base64
    s=LocalSpeakers(None,'test',tmp_path/'prints.json',process.profiles)
    s.embedding=lambda a:[1.,0.] if a[0]>0 else [0.,1.] if a[0]<0 else [.7,.7]
    clip=lambda value,seconds:base64.b64encode(np.full(int(seconds*16000),value,dtype='<i2').tobytes()).decode()
    async def run():
        client=TestClient(TestServer(create_app(process,'stone',VoiceConfig(api_key=''),online_speakers=s)))
        await client.start_server()
        try:
            origin={'Origin':str(client.make_url('/')).rstrip('/')}
            post=lambda payload:client.post('/voice-registry',json=payload,headers=origin)
            ming=(await (await post({'action':'enroll','name':'小明','clips':[clip(1000,5)]})).json())['object_id']
            await post({'action':'enroll','name':'小红','clips':[clip(-1000,5)]})
            known=dict(s.known)
            d=await (await post({'action':'try','expected':ming,'clip':clip(1000,5)})).json()
            assert d['matched']==ming and d['matched_label']=='小明' and d['ranking'][0]['score']==1.0
            d=await (await post({'action':'try','expected':'','clip':clip(0,5)})).json()
            assert d['matched'] is None
            summary=d['summary']
            assert (summary['trials'],summary['correct'],summary['missed'],summary['wrong'])==(2,2,0,0)
            assert summary['recommendation']['basis']=='no_false_accept' and summary['recommendation']['threshold']==.72
            assert s.known==known and not s.tracks
            d=await (await post({'action':'threshold','value':.72})).json()
            assert s.threshold==.72 and d['summary']['threshold']==.72
            assert (tmp_path/'prints.selftest.json').is_file()
            r=await post({'action':'try','expected':ming,'clip':clip(1000,1)})
            assert r.status==400
            d=await (await post({'action':'clear_trials'})).json()
            assert d['summary']['trials']==0 and d['summary']['recommendation'] is None
        finally:await client.close()
    asyncio.run(run())


def test_name_question_opens_only_after_player_confirms_delivery(process):
    async def run():
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            visitor=c.identities.resolve('0',cluster_id='A')
            d=c.delivery.begin('怎么称呼你？',visitor.object_id)
            assert not c.identities.name_questions
            await c.acknowledge({'reply_id':d.reply_id,'segment':0,'phase':'started'})
            assert not c.identities.name_questions
            await c.acknowledge({'reply_id':d.reply_id,'segment':0,'phase':'completed'})
            assert c.identities.name_answer(visitor,'小明')=='小明'
        finally:await c.close()
    asyncio.run(run())


def test_uncertain_audio_cannot_bind_a_previous_cloud_track_embedding(process, tmp_path):
    s=LocalSpeakers(None,'test',tmp_path/'prints.json',process.profiles)
    s.last_embedding['0']=[1.,0.]
    audio=SessionAudio(); audio.append(bytes(32000))
    t=audio.match(Transcript('你好','0',0,1000,True),s)
    assert t.identity_uncertain and not t.speaker_cluster_id
    assert not s.bind('0','lux-id')
    s.last_embedding['0']=[1.,0.]
    t=audio.match(Transcript('重叠','0',0,1000,True,overlap=True),s)
    assert t.identity_uncertain and '0' not in s.last_embedding


def test_short_voiceprint_keeps_its_length_and_newer_audio_is_placed_in_front(process, tmp_path):
    import base64
    s=LocalSpeakers(None,'manual-test',tmp_path/'prints.json',process.profiles)
    s.embedding=lambda a:[1.,0.]
    older=np.full(3*16000, 1, dtype='<i2').tobytes()
    newer=np.full(3*16000, 2, dtype='<i2').tobytes()
    stats=s.enroll_samples([older, newer],'lux-id',user_labeled=True)
    assert stats['seconds']==6.0 and len(s.known)==1
    pcm=np.frombuffer(base64.b64decode(next(iter(s.known.values()))['pcm']), dtype='<i2')
    assert len(pcm)==6*16000 and pcm[0]==2 and pcm[3*16000]==1
    extra=np.full(8*16000, 3, dtype='<i2').tobytes()
    s.embedding=lambda a:[0.,1.]
    assert s.enroll_samples([extra],'lux-id',user_labeled=True)['seconds']==10.0
    assert len(s.known)==1
    pcm=np.frombuffer(base64.b64decode(next(iter(s.known.values()))['pcm']), dtype='<i2')
    assert len(pcm)==10*16000 and pcm[0]==3 and pcm[8*16000-1]==3 and pcm[8*16000]==2
    assert next(iter(s.known.values()))['embedding']==[0.,1.]


def test_old_multiple_entries_collapse_to_one_averaged_voiceprint(process, tmp_path):
    path=tmp_path/'prints.json'
    path.write_text(json.dumps({'model_id':'manual-test','entries':{
        'a':{'object_id':'lux-id','embedding':[1.,0.],'templates':[[1.,0.]]},
        'b':{'object_id':'lux-id','embedding':[0.,1.]},
    }}), encoding='utf-8')
    s=LocalSpeakers(None,'manual-test',path,process.profiles)
    assert len(s.known)==1
    vector=next(iter(s.known.values()))['embedding']
    assert vector[0]==pytest.approx(2**-.5) and vector[1]==pytest.approx(2**-.5)
    assert 'templates' not in next(iter(s.known.values())) and 'pcm' not in next(iter(s.known.values()))


def test_selected_samples_create_normalized_persistent_centroid(process, tmp_path):
    speakers = LocalSpeakers(None, 'manual-test', tmp_path/'prints.json', process.profiles)
    lengths = []
    def embedding(samples):
        lengths.append(len(samples))
        return [2., 0.]
    speakers.embedding = embedding
    stats = speakers.enroll_samples([bytes(64000), bytes(64000)], 'lux-id')
    assert lengths == [32000, 32000, 64000]
    assert stats == {'clips': 2, 'embeddings': 1, 'seconds': 4.0, 'min_similarity': 1.0, 'enrollment_threshold': .8, 'voiceprint_seconds': 10}
    entry = next(iter(speakers.known.values()))
    assert entry['embedding'] == [1., 0.] and entry['object_id'] == 'lux-id'
    restored = LocalSpeakers(None, 'manual-test', tmp_path/'prints.json', process.profiles)
    restored.embedding = lambda samples: [1., 0.]
    assert restored.identify(np.ones(32000))[1].startswith('local:manual-test:')


def test_human_labeled_variations_collapse_to_the_single_front_voiceprint(process, tmp_path):
    path=tmp_path/'prints.json'
    s=LocalSpeakers(None,'manual-test',path,process.profiles,threshold=.95)
    kept, unused = [1.,0.], [.4,(1-.4**2)**.5]
    vectors=iter([kept, unused, kept])
    s.embedding=lambda a:next(vectors)
    stats=s.enroll_samples([bytes(96000),bytes(96000)],'lux-id',user_labeled=True)
    assert stats['min_similarity']==.4 and stats['user_labeled'] and stats['warning']
    assert len(s.known)==1 and 'templates' not in next(iter(s.known.values()))
    restored=LocalSpeakers(None,'manual-test',path,process.profiles,threshold=.95)
    restored.embedding=lambda a:kept
    assert restored.identify(np.ones(32000))[1].startswith('local:manual-test:')
    restored.embedding=lambda a:unused
    assert not restored.identify(np.ones(32000))[1]


def test_matching_slider_changes_recognition_and_survives_restart(process, tmp_path):
    path=tmp_path/'prints.json'
    speakers=LocalSpeakers(None,'model',path,process.profiles)
    speakers.embedding=lambda a:[1.,0.]
    track,_,_=speakers.identify([0]*48000)
    assert speakers.bind(track,'lux-id')
    speakers.embedding=lambda a:[.64, (.5904)**.5]
    assert not speakers.identify([])[1]
    speakers.set_threshold(.60)
    for _ in range(9):
        assert not speakers.identify([0]*32000)[1]
    assert speakers.identify([0]*32000)[1]
    assert speakers.margin==.08 and speakers.enrollment_threshold==.8
    assert LocalSpeakers(None,'model',path,process.profiles).threshold==.60
    assert LocalSpeakers(None,'other-model',path,process.profiles).threshold==.65
    for bad in (.1,1,float('nan'),True,'0.6'):
        with pytest.raises(ValueError):speakers.set_threshold(bad)
    assert speakers.threshold==.60


def test_different_people_selected_together_are_not_registered(process, tmp_path):
    speakers = LocalSpeakers(None, 'manual-test', tmp_path/'prints.json', process.profiles)
    vectors = iter(([1., 0.], [0., 1.]))
    speakers.embedding = lambda samples: next(vectors)
    with pytest.raises(ValueError, match='一致性不足') as error:
        speakers.enroll_samples([bytes(64000), bytes(64000)], 'lux-id')
    assert error.value.sample_indices == (0, 1)
    assert not speakers.known and not speakers.path.exists()


def test_short_clips_cannot_hide_multiple_people_by_concatenation(process, tmp_path):
    speakers = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles)
    with pytest.raises(ValueError, match='不足 1.5') as error:
        speakers.enroll_samples([bytes(16000)] * 8, 'lux-id')
    assert error.value.sample_indices == tuple(range(8))
    assert not speakers.known


def test_long_clip_checks_internal_speaker_changes(process, tmp_path):
    speakers = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles)
    vectors = iter(([1., 0.], [0., 1.]))
    speakers.embedding = lambda samples: next(vectors)
    with pytest.raises(ValueError, match='第 1 段') as error:
        speakers.enroll_samples([bytes(192000)], 'lux-id')
    assert error.value.sample_indices == (0,)
    assert not speakers.known


def test_manual_selection_uses_retained_audio_and_does_not_bind_cloud_track(process, tmp_path):
    async def run():
        speakers = LocalSpeakers(None, 'manual-test', tmp_path/'prints.json', process.profiles)
        speakers.embedding = lambda samples: [1., 0.]
        audio = SessionAudio(seconds=3)
        audio.append(bytes(96000))
        events = []
        async def send(m): events.append(m)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, local_speakers=speakers, sample_source=audio, input_pause_s=.02)
        try:
            await c.accept(Transcript('这是一段声音', '7', 0, 3000, True))
            await asyncio.wait_for(c.queue.join(), 1)
            await asyncio.wait_for(c.turn_queue.join(), 2)
            iid = next(iter(c.input_records))
            audio.append(bytes(128000))  # original sample leaves rolling ASR buffer
            assert audio.segment(0, 2000) is None
            await c.enroll_inputs([iid], 'lux')
            assert events[-1]['type'] == 'enrollment'
            assert c.scene[0].speaker.object_id == 'lux-id'
            assert c.scene[0].speaker.method == 'manual_annotation'
            assert c.identities.resolve('7').status == 'unknown'
            assert next(iter(speakers.known.values()))['object_id'] == 'lux-id'
            await c.enroll_inputs(['expired'], 'lux')
            assert events[-1]['type'] == 'enrollment_error'
        finally:
            await c.close()
        assert not c.input_records
    asyncio.run(run())


def test_overlapping_sample_cannot_be_enrolled(process, tmp_path):
    async def run():
        speakers = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles)
        audio = SessionAudio(); audio.append(bytes(64000))
        events = []
        async def send(m): events.append(m)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, local_speakers=speakers, sample_source=audio)
        try:
            await c.accept(Transcript('混合声音', '7', 0, 2000, True, overlap=True))
            await c.enroll_inputs(list(c.input_records), 'lux')
            assert events[-1]['type']=='enrollment_error' and '重叠' in events[-1]['text']
            assert not speakers.known
        finally: await c.close()
    asyncio.run(run())


def test_prompt_sections_and_call_selection_are_actual_saved_records(process):
    async def run():
        events=[]
        async def send(m): events.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send,input_pause_s=.02)
        try:
            await c.accept(Transcript('你好','A',0,2000,True))
            await asyncio.wait_for(c.queue.join(),1)
            await asyncio.wait_for(c.turn_queue.join(),2)
            activity=c.last_debug['activity_id']
            process.step_inputs.append_call(subject_id='stone', activity_id=activity, purpose='write_zone', model='test',
                system_text='【测试区块】\n只有实际记录\n【另一块】\n尾部\nJSON Schema：{}', user_text='独立写场输入')
            await c.diagnostics('prompt', activity_id=activity, purpose='write_zone', part='测试区块')
            assert '只有实际记录' in events[-1]['text'] and '尾部' not in events[-1]['text']
            assert '测试区块' in events[-1]['sections']
            await c.diagnostics('prompt', purpose='write_zone', part='user')
            assert '独立写场输入' in events[-1]['text']
            for section in ('input','scene','identity','delivery','jev','response','timing'):
                await c.diagnostics(section)
                json.loads(events[-1]['text'])
            assert 'queue_wait_ms' in events[-1]['text']
        finally: await c.close()
    asyncio.run(run())


def test_internal_names_stay_unique_and_typed_correction_keeps_the_person(process, tmp_path):
    from jshi.core.envelope import SceneUtterance
    from jshi.voice.identity import name_correction
    clock={'t':1_000_000.0}
    speakers=LocalSpeakers(None,'test',tmp_path/'prints.json',process.profiles,clock=lambda:clock['t'])
    speakers.embedding=lambda a:[1.,0.]
    async def run():
        events=[]
        async def send(m):events.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send,local_speakers=speakers,input_pause_s=.01)
        try:
            visitor=c.identities.resolve('t',cluster_id='A')
            assert visitor.label=='未命名访客 1'
            for _ in range(10):
                track=speakers.identify([0]*48000)[0]
            speakers.tracks[visitor.track_id]=speakers.tracks[track]
            speakers.last_embedding[visitor.track_id]=speakers.last_embedding[track]
            assert speakers.remember_anonymous(visitor.track_id,visitor.object_id)
            c.scene.append(SceneUtterance('你好',visitor,0,1000))
            await c.typed('我是小明')
            assert events[-1]['type']=='rename_prompt' and events[-1]['name']=='小明'
            await c.apply_rename(visitor.object_id,'小明',True)
            named=process.profiles.get(visitor.object_id)
            assert named.label=='小明' and '未命名访客 1' in named.aliases
            assert 'expires_at' not in next(iter(speakers.known.values()))
            another=c.identities.resolve('u',cluster_id='B')
            assert another.label=='未命名访客 2'
            await c.typed('我其实是小敏，不是小明')
            await c.apply_rename(visitor.object_id,'小敏',True)
            assert process.profiles.get(visitor.object_id).label=='小敏'
            assert name_correction('我其实是小红，不是别人','小敏',anonymous=False)==''
            await c.typed('今天想出门')
            await asyncio.wait_for(c.queue.join(),1)
            assert c.scene[-1].text=='今天想出门' and c.scene[-1].speaker.label=='小敏'
            clock['t']+=8*86400
            later=LocalSpeakers(None,'test',tmp_path/'prints.json',process.profiles,clock=lambda:clock['t'])
            later.embedding=lambda a:[1.,0.]
            _,vpid,_=later.identify([0]*48000)
            assert vpid.startswith('local:')
        finally:await c.close()
    asyncio.run(run())


def test_unnamed_voice_expires_when_not_met_again(process, tmp_path):
    clock={'t':1_000_000.0}
    speakers=LocalSpeakers(None,'test',tmp_path/'prints.json',process.profiles,clock=lambda:clock['t'])
    speakers.embedding=lambda a:[1.,0.]
    from jshi.voice.identity import VoiceIdentities
    visitor=VoiceIdentities(process.profiles,'stone','s').resolve('t',cluster_id='A')
    track=speakers.identify([0]*48000)[0]
    speakers.last_embedding[visitor.track_id]=speakers.last_embedding[track]
    assert speakers.remember_anonymous(visitor.track_id,visitor.object_id)
    clock['t']+=8*86400
    later=LocalSpeakers(None,'test',tmp_path/'prints.json',process.profiles,clock=lambda:clock['t'])
    later.embedding=lambda a:[1.,0.]
    assert later.identify([0]*48000)[1]==''
    assert later.known=={}


def test_old_merged_audio_with_small_silence_gaps_is_not_a_new_turn():
    assembler=TranscriptAssembler()
    rows=[{'text':'妈，我出去玩的那只呢','start_time':209900,'end_time':213800,'speaker_id':'0','definite':True},
          {'text':'什么叫你出去玩','start_time':214800,'end_time':215700,'speaker_id':'7','definite':True},
          {'text':'我出去玩带的那只笔呢','start_time':215800,'end_time':217800,'speaker_id':'0','definite':True}]
    assert len(assembler.accept({'result':{'utterances':rows}}))==3
    merged={'text':'妈我出去玩的那只呢什么叫你出去玩我出去玩带的那只笔呢','start_time':209900,'end_time':217800,'speaker_id':'0','definite':True}
    assert assembler.accept({'result':{'utterances':[merged]}})==()
