"""Describe agreement without preassigning a speaker or mixing score scales."""
import math
from enum import Enum

MLP_SCORE_FLOOR = .92


class IdentitySource(str,Enum):
    MANUAL='人工'
    SELF='自述'
    VOICEPRINT='声纹'
    MLP='MLP'
    JEV='JEV'
    COMBINED='综合'
    UNKNOWN='未知'


class IdentityState(str,Enum):
    CONFIRMED='确定'
    POSSIBLE='可能'
    PENDING='待定'


def compare_identity(voice_candidates, classifier,*,voice_threshold=.33,voice_margin=.08):
    def valid(row):
        return bool(row.get('object_id')) and type(row.get('score')) in (int,float) and math.isfinite(row['score'])
    voices=sorted((r for r in voice_candidates if valid(r) and r.get('source','voiceprint') in
        {'voiceprint','voiceprint_match','repeated_short_audio'}),key=lambda r:r['score'],reverse=True)
    models=sorted((r for r in classifier.get('ranking',[]) if valid(r) and 0<=r['score']<=1),
        key=lambda r:r['score'],reverse=True)
    if not models:
        return {'state':'unavailable'}
    model=models[0]
    def threshold(r):
        value=r.get('threshold',.8)
        return max(MLP_SCORE_FLOOR, value) if type(value) in (int,float) and math.isfinite(value) else MLP_SCORE_FLOOR
    high=(model['score']>=threshold(model) and all(r['score']<threshold(r) for r in models[1:])
        and (len(models)==1 or model['score']-models[1]['score']+1e-9>=.1))
    mlp={k:model[k] for k in ('object_id','label','score','threshold','verified') if k in model}
    if not voices:
        return {'state':'mlp_only','mlp_high':high,'mlp_priority':high,'mlp':mlp}
    voice=voices[0]
    reliable=(type(voice_threshold) in (int,float) and type(voice_margin) in (int,float)
        and voice.get('source')!='repeated_short_audio' and voice['score']>=voice_threshold
        and (len(voices)==1 or voice['score']-voices[1]['score']+1e-9>=voice_margin))
    return {'state':'agreement' if voice['object_id']==model['object_id'] else 'conflict',
        'voice_reliable':reliable,'mlp_priority':high and not reliable,
        'mlp_high':high,'voice':{k:voice[k] for k in ('object_id','label','score','source') if k in voice},
        'mlp':mlp}


def identity_display(speaker,comparison,*,certainty='',original=None,classifier=None):
    """A displayed likely candidate is distinct from confirmed conversation attribution."""
    original=original or speaker
    selected=speaker.get('object_id','')
    method=speaker.get('method','')
    confirmed=speaker.get('status')=='recognized' or certainty=='确定'
    mlp=comparison.get('mlp',{})
    def view(label,source,state,score=None,reason=''):
        return {'label':label,'source':IdentitySource(source).value,'state':IdentityState(state).value,'score':score,'reason':reason}
    if method in {'manual_annotation','user_selected'}:
        return view(speaker.get('label',''),'人工','确定',reason='用户人工指定')
    if method=='self_introduction' or speaker.get('status')=='introduced':
        return view(speaker.get('label',''),'自述','确定',reason='来自本人明确自我介绍')
    if confirmed:
        if original.get('method')=='voiceprint_match' and original.get('object_id')==selected:
            return view(speaker['label'],'声纹','确定',original.get('confidence'),'原声纹可靠命中；分数是相似度')
        if method=='personal_classifier':
            return view(speaker['label'],'MLP','确定',speaker.get('confidence'),'已验证个人分类器命中；分数不是身份概率')
        source='综合' if mlp.get('object_id')==selected and comparison.get('mlp_high') else 'JEV'
        return view(speaker['label'],source,'确定',reason='JEV最终确认；综合表示同时有MLP支持，无独立数值概率')
    if classifier and (classifier.get('overlap') or any(r.get('stale_labels') for r in classifier.get('ranking',[]))):
        mixed = compare_identity([], classifier)
        if mixed.get('mlp_priority') and (classifier.get('overlap') or any(
                r.get('object_id') == mixed.get('mlp',{}).get('object_id') and r.get('stale_labels')
                for r in classifier.get('ranking',[]))):
            candidate = mixed['mlp']
            stale = any(r.get('object_id') == candidate['object_id'] and r.get('stale_labels')
                        for r in classifier.get('ranking',[]))
            return view(candidate.get('label',''),'MLP','可能',candidate['score'],
                '旧版参数高分参考；训练标签已更新，等待新版模型，不作自动确认' if stale else
                '混合录音中MLP单一高分领先，仅显示可能候选；未确认所有声音来自此人；分数不是身份概率')
    if comparison.get('mlp_priority'):
        return view(mlp.get('label',''),'MLP','可能',mlp['score'],
            '声纹未可靠命中，MLP单一高分领先；句首仅显示优先候选，尚未确认；分数不是身份概率')
    if speaker.get('status')=='provisional' or certainty=='可能':
        source='JEV' if method in {'jev_attribution','context_attribution'} else 'MLP' if method.startswith('personal_classifier') else '声纹'
        return view(speaker.get('label',''),source,'可能',reason='当前仅支持可能；JEV未提供数值概率')
    return view('归属待定','未知','待定',reason='当前证据不足，未确认姓名')
