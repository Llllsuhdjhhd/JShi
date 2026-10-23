import asyncio
import json

import pytest

from jshi.app.voice import VoiceConversation
from jshi.voice.personal import PersonalSpeakers
from jshi.voice.personal_model import load_weights
from jshi.voice.personal_store import DEFAULTS, OTHER_VOICE, PersonalStore
from jshi.voice.personal_train import groups, readiness, split_plan, train
from jshi.voice.volc import Transcript
from tests.test_personal_speakers import audio, speakers, make_training_records
from tests.test_voice import FakeCloud, process


def records(n=20):
    return [{'id':f'{label}-{i}', 'session_id':'one-conversation', 'label':label,
        'source':'manual', 'vectors':[[float(label),float(1-label)]], 'durations':[.5]}
        for label in (0,1) for i in range(n)]


def test_one_conversation_can_train_with_disjoint_original_recordings():
    rows=records()
    splits, mode=split_plan(rows)
    assert mode=='recording' and readiness(rows,DEFAULTS)[0]
    sets=[{r['id'] for r in splits[k]} for k in splits]
    assert all(not a&b for i,a in enumerate(sets) for b in sets[i+1:])
    assert set.union(*sets)=={r['id'] for r in rows}
    assert split_plan(make_training_records())[1]=='session'


@pytest.mark.parametrize('n',[1,12])
def test_actual_tiny_training_outputs_weights_and_explicit_trial_state(tmp_path,n):
    pytest.importorskip('torch')
    path=tmp_path/'snapshot.json';output=tmp_path/'model.npz'
    path.write_text(json.dumps({'records':records(n),'config':DEFAULTS,'dimension':2}),encoding='utf-8')
    result=train(path,output)
    assert result['status']=='ready' and result['verified'] is False
    assert result['validation_mode']==('training_only' if n==1 else 'recording')
    assert all(not p['identity_enabled'] for p in result['duration_reports'].values())
    load_weights(output,2)
    json.dumps(result,allow_nan=False)


def test_batch_default_upgrades_to_ten_without_overwriting_custom_choice(tmp_path):
    assert PersonalStore(tmp_path/'new').settings()['batch_samples']==10
    for old,expected in [(16,10),(30,30)]:
        store=PersonalStore(tmp_path/str(old))
        with store.connect() as db:
            db.execute("DELETE FROM meta WHERE key='small_sample_training'")
            config=store.value(db,'settings');config['batch_samples']=old;store.put(db,'settings',config)
        assert PersonalStore(store.root).settings()['batch_samples']==expected


def test_other_category_is_negative_for_every_person_and_never_a_profile(process,tmp_path):
    engine=PersonalSpeakers(tmp_path/'personal',process.profiles)
    before={p.object_id for p in process.profiles.list()}
    try:
        sid=engine.capture(audio(),OTHER_VOICE,'session','tv',speakers(),source='manual')
        for oid in ['lux-id','any-other-target']:
            row=next(r for r in engine.labelled_samples(oid) if r['id']==sid)
            assert row['label']==0 and row['other'] and row['source']=='manual'
        assert before=={p.object_id for p in process.profiles.list()}
        with pytest.raises(ValueError):engine.set_target(OTHER_VOICE)
        engine.store.withdraw('session','tv')
        assert not engine.labelled_samples('lux-id')
    finally:engine.close()


def test_tenth_new_recording_triggers_batch_and_manual_can_bypass_it(process,tmp_path,monkeypatch):
    engine=PersonalSpeakers(tmp_path/'personal',process.profiles)
    engine.select_targets(['lux-id'])
    submitted=[]
    monkeypatch.setattr(engine.trainer,'submit',lambda *a:submitted.append(a))
    try:
        for i in range(9):engine.store.save(audio(value=1000+i),'lux-id','s',str(i),source='manual')
        assert not engine.request_training('lux-id',speakers())
        engine.store.save(audio(),'lux-id','s','10',source='manual')
        assert engine.request_training('lux-id',speakers()) and len(submitted)==1
        engine.training.clear()
        with engine.store.connect() as db:
            engine.store.state('lux-id','collecting',attempted_revision=engine.store.revision()[0])
        assert not engine.request_training('lux-id',speakers())
        assert engine.request_training('lux-id',speakers(),force=True)
    finally:engine.close()


def test_trial_model_outputs_scores_and_retraining_keeps_backup(process,tmp_path):
    pytest.importorskip('torch')
    engine=PersonalSpeakers(tmp_path/'personal',process.profiles)
    engine.select_targets(['lux-id'])
    path=tmp_path/'snapshot.json'
    snapshot={'records':records(6),'config':DEFAULTS,'dimension':2,'object_id':'lux-id',
        'model_id':'fixture-encoder','revision':0,'label_epoch':engine.store.revision()[1]}
    try:
        first=None
        for i in range(2):
            out=tmp_path/f'model{i}.npz'
            if first:snapshot['previous']={'path':str(engine._directory('lux-id')/(first['version']+'.npz')),
                'threshold':first['threshold'],'verified':False}
            path.write_text(json.dumps(snapshot),encoding='utf-8')
            result=train(path,out);engine.publish('lux-id',snapshot,out,result)
            if first:assert engine.manifest('lux-id')['previous']['version']==first['version']
            else:first=engine.manifest('lux-id')['active']
        prediction=engine.predict(audio(),speakers())
        assert prediction['ranking'] and prediction['ranking'][0]['verified'] is False
        assert not prediction['matched']
        async def check_inline_trial():
            from jshi.voice.audio_buffer import SessionAudio
            sent=[]
            async def send(m):sent.append(m)
            source=SessionAudio();source.append(audio())
            c=VoiceConversation(process,'stone',FakeCloud(),send,personal_speakers=engine,
                local_speakers=speakers(),sample_source=source,input_pause_s=.01)
            try:
                await c.accept(Transcript('试用分数展示','unidentified-trial',0,500,True))
                await asyncio.wait_for(c.queue.join(),3)
                event=[m for m in sent if m['type']=='personal_prediction'][-1]
                assert event['personal_prediction']['ranking'][0]['verified'] is False
                assert not event['personal_prediction']['matched']
                evidence=[r for e in c.jev_scene_store.snapshot()['events']
                    for r in e['payload'].get('candidates',[]) if r.get('source')=='personal_classifier']
                assert evidence and all(r['verified'] is False for r in evidence)
                assert all('classification_score' in r and 'similarity' not in r for r in evidence)
            finally:await c.close()
        asyncio.run(check_inline_trial())
    finally:engine.close()


def test_inline_other_saves_undo_without_binding_identity(process,tmp_path):
    engine=PersonalSpeakers(tmp_path/'personal',process.profiles)
    async def run():
        sent=[]
        async def send(m):sent.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send,personal_speakers=engine,local_speakers=speakers())
        c.input_records['tv']={'pcm':audio(),'transcript':Transcript('电视里的人声','tv-track',0,500,True,input_id='tv')}
        try:
            await c.label_personal_inputs(['tv'],OTHER_VOICE)
            assert any(m['type']=='personal_labelled' and m['negative_only'] for m in sent)
            assert 'tv' not in c.annotations
            assert OTHER_VOICE not in c.code_owner.values()
            assert len(engine.labelled_samples('lux-id'))==1
            await c.unlabel_personal_inputs(['tv'])
            assert not engine.labelled_samples('lux-id')
        finally:await c.close()
    try:asyncio.run(run())
    finally:engine.close()


def test_other_upload_is_allowed_but_cannot_be_selected_as_model_target(process,tmp_path):
    import base64
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    engine=PersonalSpeakers(tmp_path/'personal',process.profiles)
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig(''),
                online_speakers=speakers(),personal_speakers=engine))) as client:
            origin={'Origin':str(client.make_url('/')).rstrip('/')}
            r=await client.post('/api/personal-speakers',headers=origin,json={'action':'sample',
                'object_id':OTHER_VOICE,'session_id':'tv','input_id':'clip',
                'pcm':base64.b64encode(audio()).decode()})
            assert r.status==200,await r.text()
            assert engine.labelled_samples('lux-id')[0]['label']==0
            r=await client.post('/api/personal-speakers',headers=origin,
                json={'action':'target','object_id':OTHER_VOICE})
            assert r.status==400 and not engine.store.targets()
    try:asyncio.run(run())
    finally:engine.close()
