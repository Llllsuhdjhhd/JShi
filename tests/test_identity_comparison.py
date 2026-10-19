import asyncio
import json

from jshi.app.voice import VoiceConversation
from jshi.core.envelope import SceneUtterance,SpeakerEvidence
from jshi.models import ModelResponse
from jshi.recognition import ObjectProfile
from jshi.voice.identity_comparison import compare_identity
from jshi.voice.jev import VoiceJEV
from tests.test_voice import FakeCloud,process


def test_comparison_uses_all_models_without_preselecting_voice_top():
    voice=[{'object_id':'lux','label':'陆兴','score':.25,'source':'voiceprint'}]
    model={'ranking':[{'object_id':'lux','label':'陆兴','score':.02,'threshold':.8,'verified':False},
                      {'object_id':'lg','label':'陆广','score':.99,'threshold':.8,'verified':False}]}
    result=compare_identity(voice,model)
    assert result['state']=='conflict' and result['mlp']['object_id']=='lg' and result['mlp_high']
    model['ranking'][0]['score']=.999
    assert not compare_identity(voice,model)['mlp_high']  # both high is ambiguous
    model['ranking'][1]['score']=.01
    result=compare_identity(voice,model)
    assert result['state']=='agreement' and result['mlp_high']
    assert compare_identity([],model)['state']=='mlp_only'
    assert compare_identity([],model)['mlp_priority']
    assert compare_identity(voice,{})=={'state':'unavailable'}


def test_trial_scores_reach_jev_as_separate_evidence_and_allow_semantic_confirmation(process):
    process.profiles.create(ObjectProfile('another-person-long-id','陆广',status='confirmed'))
    class Model:
        def generate(self,request):
            self.request=request
            payload=json.loads(request.input_text);line=payload['input']['lines'][0]
            assert line['joint_evidence']['state']=='agreement' and line['joint_evidence']['mlp_high']
            assert line['voice_ranking']['similarity']==.223
            mlps=[c for c in line['candidates'] if c['source']=='personal_classifier']
            assert len(mlps)==2 and all(c['validation']=='limited' for c in mlps)
            assert all('classification_score' in c and 'similarity' not in c for c in mlps)
            pick=max(mlps,key=lambda r:r['classification_score'])['p']
            return ModelResponse(model='fixture',text=json.dumps({'items':[{'i':1,'person':pick,
                'certainty':'confirmed','why':'声纹与个人分类同向，当前片场和接话关系一致'}],
                'level':3,'recall_memory':False,'main_image':'omit','control':'redirect','route':'main','reply':''},ensure_ascii=False))
    async def run():
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            c.input_records['clip']={'personal_classifier':{'status':'uncertain','matched':'','ranking':[
                {'object_id':'lux-id','label':'lux','score':.999,'threshold':.8,'verified':False,'version':'secret-version'},
                {'object_id':'another-person-long-id','label':'陆广','score':.001,'threshold':.8,'verified':False}]}}
            c.candidate_cache['clip']=[{'object_id':'lux-id','label':'lux','score':.223,'source':'voiceprint'}]
            u=SceneUtterance('真的吗',SpeakerEvidence('A','input:clip','声音归属待定','unknown','unassigned_audio'),0,800,input_id='clip')
            model=Model();batch=c._jev_batch([u])
            decision=VoiceJEV(model).decide_batch('stone',batch,[],{'state':'idle','jev_scene':'lux正在与匠石对话'})
            assert decision.judged and decision.items[0].speaker_level=='确定'
            final=c._apply_attribution([u],decision.items)
            assert final[0].speaker.object_id=='lux-id' and final[0].speaker.status=='recognized'
            assert not c.input_records['clip']['personal_classifier']['matched']
            assert 'another-person-long-id' not in model.request.input_text
            assert 'secret-version' not in model.request.input_text
        finally:await c.close()
    asyncio.run(run())


def test_high_single_mlp_is_display_candidate_but_cannot_override_confirmed_identity():
    from jshi.voice.identity_comparison import identity_display,IdentitySource,IdentityState
    unknown={'object_id':'input:1','label':'声音归属待定','method':'unassigned_audio','status':'unknown'}
    classifier={'ranking':[{'object_id':'qf','label':'qf','score':1.,'threshold':.8,'verified':False},
        {'object_id':'lg','label':'luguang','score':0.,'threshold':.8,'verified':False}]}
    voices=[{'object_id':'lg','label':'luguang','score':.265,'source':'voiceprint'}]
    comparison=compare_identity(voices,classifier)
    assert comparison['state']=='conflict' and comparison['mlp_priority']
    display=identity_display(unknown,comparison)
    assert (display['label'],display['source'],display['state'],display['score'])==('qf','MLP','可能',1.)
    voices[0]['score']=.65
    comparison2=compare_identity(voices,classifier)
    assert comparison2['voice_reliable'] and not comparison2['mlp_priority']
    assert identity_display(unknown,comparison2)['source']=='未知'
    manual={**unknown,'object_id':'lg','label':'luguang','method':'manual_annotation','status':'recognized'}
    assert identity_display(manual,comparison)['source']=='人工'
    jev={**unknown,'object_id':'qf','label':'qf','method':'jev_attribution','status':'recognized'}
    display=identity_display(jev,comparison,certainty='确定',original=unknown)
    assert display['source']=='综合' and display['score'] is None
    for speaker in [unknown,manual,jev]:
        d=identity_display(speaker,comparison)
        assert d['source'] in {s.value for s in IdentitySource}
        assert d['state'] in {s.value for s in IdentityState}


def test_mixed_high_mlp_displays_candidate_without_promoting_confirmation(process):
    async def run():
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            unknown=SpeakerEvidence('a','input:mix','声音归属待定','unknown','unassigned_audio')
            c.input_records['mix']={'personal_classifier':{'overlap':True,'matched':'','ranking':[
                {'object_id':'lux-id','label':'lux','score':1.,'threshold':.8,'verified':False},
                {'object_id':'other','label':'other','score':0.,'threshold':.8,'verified':False}]}}
            comparison=c.identity_comparison('mix')
            assert not comparison['mlp_high'] and not comparison['mlp_priority']
            display=c.identity_display('mix',unknown)
            assert (display['label'],display['source'],display['state'],display['score'])==('lux','MLP','可能',1.)
            assert '混合录音' in display['reason']
            assert not c.input_records['mix']['personal_classifier']['matched']
            manual=SpeakerEvidence('a','other','other','recognized','manual_annotation')
            assert c.identity_display('mix',manual)['source']=='人工'
            c.input_records['mix']['personal_classifier']['ranking'][1]['score']=.99
            assert c.identity_display('mix',unknown)['state']=='待定'
        finally:await c.close()
    asyncio.run(run())


def test_candidate_floor_is_092_and_keeps_model_threshold_and_gap():
    model={'ranking':[{'object_id':'a','label':'A','score':.9199,'threshold':.8},
        {'object_id':'b','label':'B','score':0.,'threshold':.8}]}
    assert not compare_identity([],model)['mlp_priority']

    model['ranking'][0]['score']=.92
    assert compare_identity([],model)['mlp_priority']
    model['ranking'][1]['score']=.91
    assert not compare_identity([],model)['mlp_priority']
    model['ranking'][1]['score']=0.
    model['ranking'][0]['threshold']=.95
    assert not compare_identity([],model)['mlp_priority']


def test_stale_model_outputs_are_reference_candidates_not_strong_jev_evidence(process):
    async def run():
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            c.input_records['old']={'personal_classifier':{'ranking':[
                {'object_id':'lux-id','label':'lux','score':.999,'threshold':.92,'stale_labels':True,'verified':False}]}}
            unknown=SpeakerEvidence('a','input:old','声音归属待定','unknown','unassigned_audio')
            comparison=c.identity_comparison('old')
            assert not comparison['mlp_high'] and not comparison['mlp_priority']
            display=c.identity_display('old',unknown)
            assert display['label']=='lux' and display['state']=='可能' and '旧版' in display['reason']
        finally:await c.close()
    asyncio.run(run())
