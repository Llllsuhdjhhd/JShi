"""Restartable scoped identity updates; no name-based global rewriting."""
import hashlib
from pathlib import Path
from jshi.voice.dialogue import dialogue_state
from .acquaintance import acquaintance_store

MODULES=('unknown_inputs','dialogue','scene','main_scene','samples','voiceprints','memory','experience','tasks')


def apply_link(process, link_id, *, personal=None, live_scene=None, local=None):
    store=acquaintance_store(process)
    facts=store.facts(link_id)
    def destination(row): return row['target'] if row['active'] else row['source']
    def label(row):
        profile=process.profiles.get(destination(row))
        return profile.label if profile else facts.get('source_label','未知人物')
    def unknown(row):
        if row['active']: process.unknown_inputs.bind(row['inputs'],row['target'])
        else:
            for iid in row['inputs']:
                process.unknown_inputs.bind((iid,),facts.get('bindings',{}).get(iid))
    def dialogue(row):
        dialogue_state(process).link_inputs(row['subject'],row['inputs'],row['target'],label(row),
            link_id=row['id'],active=bool(row['active']))
    def scene(row):
        from jshi.voice.jev_scene import JEVSceneStore
        session=facts.get('session')
        if not session or not session.isalnum(): raise ValueError('缺少有效的原会话范围')
        path=process.repository.path.parent/'jev_scenes'/hashlib.sha256(row['subject'].encode()).hexdigest()[:16]/(session+'.json')
        target_store=live_scene if live_scene is not None and live_scene.path==path else JEVSceneStore(path)
        codes=dict(target_store.state['codes']); labels=dict(target_store.state['labels'])
        for code,person in facts.get('codes',{}).items():
            if code in codes and codes[code]!=person: raise ValueError('人物短代号映射存在冲突，保留待复核')
            codes[code]=person
        labels.update(facts.get('labels',{}))
        oid=destination(row)
        code=next((c for c,p in codes.items() if p==oid),None)
        if code is None:
            n=1
            while 'P'+str(n) in codes: n+=1
            code='P'+str(n);codes[code]=oid
        labels[oid]=label(row)
        target_store.correct({'basis':'manual_annotation' if row['active'] else 'manual_annotation_undo',
            'scope':'selected_events_only','p':code,'text':('确认' if row['active'] else '撤销此前关联，恢复原归属：')+label(row),
            'corrected_events':[{'ref':i,'person':code,'text':facts.get('texts',{}).get(i,'')} for i in row['inputs']]},
            codes=codes,labels=labels)
    def main(row):
        process.note_scene_delivery(row['subject'],'person-link:'+row['id']+':'+str(row['active']),
            '（人物归属更新）'+('已确认归属：' if row['active'] else '已撤销先前关联，恢复原归属：')+label(row)+'；仅涉及原会话中的以下发言：'+
            '；'.join(facts.get('texts',{}).get(i,'〔原话保留在原记录〕') for i in row['inputs'])+
            '。原话、时间不变，不追认其他未知人物。')
    def samples(row):
        if personal is None:
            # No loaded personal model: persisted dataset changes are still safe to apply.
            from jshi.voice.personal_store import PersonalStore
            root=process.repository.path.parent/'personal_speakers'/hashlib.sha256(row['subject'].encode()).hexdigest()[:16]
            if not (root/'samples.sqlite3').exists(): return
            sample_store=PersonalStore(root)
        else: sample_store=personal.store
        sample_store.link_inputs(row['id'],row['inputs'],facts.get('session',''),row['target'],active=bool(row['active']),baselines=facts.get('sample_baselines'))
    def voiceprints(row):
        if row['active']: return  # Enrollment itself has already validated the actual audio.
        speaker=local
        if speaker is None and facts.get('voice_bank'):
            from jshi.voice.speakers import LocalSpeakers
            bank=(process.repository.path.parent/facts['voice_bank']).resolve()
            if not bank.is_relative_to(process.repository.path.parent.resolve()): raise ValueError('声纹库位置越界')
            if bank.exists(): speaker=LocalSpeakers(None,facts['voice_model'],bank,process.profiles)
        if speaker is not None and hasattr(speaker,'withdraw_reference_inputs'):
            speaker.withdraw_reference_inputs(row['target'],facts.get('session',''),row['inputs'])

    def projection(row):
        # Readers consult the same active-link ledger, so revoke also invalidates views.
        if getattr(process.memory,'person_links',None) is not store: process.memory.person_links=store
        if process.long_term_experience is not None: process.long_term_experience.person_links=store
        process.tool_service.hang_store.person_links=store
    store.apply(link_id,dict(unknown_inputs=unknown,dialogue=dialogue,scene=scene,main_scene=main,
        samples=samples,voiceprints=voiceprints,memory=projection,experience=projection,tasks=projection))
    return store.jobs(link_id)


def retry_links(process, subject, **kwargs):
    for key in acquaintance_store(process).pending(subject): apply_link(process,key,**kwargs)
