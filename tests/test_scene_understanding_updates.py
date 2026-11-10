import json
from dataclasses import replace

import pytest

from jshi.core.scene_updates import parse_scene_updates
from jshi.core.main_input import entry_assessments
from jshi.models import ModelResponse
from jshi.skill.cognition import _persona_to_model_response, _to_model_response
from jshi.subject.pending_scene import PendingScene
from jshi.voice.jev_scene import JEVSceneStore, recent_material
from jshi.voice.jev_material import support_for_main
from tests.test_voice import process


UPDATE = {'scope': '这段设备讨论', 'understanding': '更可能是两人彼此讨论，先前插话不证明受邀',
          'basis': '此前误当成对自己的提问；随后两人继续彼此问答，没有承接我的问题'}


def test_update_survives_independent_scene_acknowledgements(tmp_path):
    pending = PendingScene(tmp_path / 'pending.json')
    pending.append('stone', 'old', '匠石已经说过：我来处理。')
    pending.append_updates('stone', 'turn', [UPDATE], input_ids=('actual-input-id',))
    pending.append_updates('stone', 'turn', [UPDATE])
    event, = pending.shared_events('stone')
    assert event['source'] == 'main_cognition'
    assert len(pending.snapshot('stone')) == 2
    # Main finishes before JEV reads: only its pending queue is acknowledged.
    pending.complete(['old', event['id']])
    restored = PendingScene(pending.path)
    assert restored.snapshot('stone') == ()
    assert restored.shared_events('stone') == (event,)
    jev = JEVSceneStore(tmp_path / 'jev.json')
    previous = jev.snapshot()
    assert jev.sync_shared_events(restored.shared_events('stone')) == 1
    assert not jev.commit(previous, '旧解释不能晚到覆盖修正')
    snap = jev.snapshot()
    material, _ = recent_material(snap, '')
    assert material[0]['kind'] == 'understanding_update'
    assert 'actual-input-id' not in json.dumps(material)
    assert jev.commit(snap, '匠石曾插话；后续两人问答说明这段是旁聊。')
    assert jev.sync_shared_events(restored.shared_events('stone')) == 0
    assert not jev.snapshot()['events']
    assert restored.shared_events('stone') == (event,)


def test_shared_update_save_failure_rolls_back_both_views(tmp_path, monkeypatch):
    pending = PendingScene(tmp_path / 'pending.json')
    def failed():
        raise OSError('disk full')
    monkeypatch.setattr(pending, '_save', failed)
    with pytest.raises(OSError):
        pending.append_updates('stone', 'turn', [UPDATE])
    assert not pending.shared_events('stone') and not pending.snapshot('stone')


def test_list_file_migration_and_subject_scope(tmp_path):
    p = tmp_path / 'pending.json'
    p.write_text(json.dumps([{'subject_id':'stone', 'id':'old', 'text':'已经发生的原话'}]), encoding='utf-8')
    pending = PendingScene(p)
    pending.append_updates('stone', 'turn', [UPDATE])
    assert not pending.shared_events('other')
    assert pending.snapshot('stone')[0]['text'] == '已经发生的原话'
    assert len(PendingScene(p).snapshot('stone')) == 2


def test_new_understanding_visible_without_importing_future_human_speech(tmp_path):
    store = JEVSceneStore(tmp_path/'jev.json')
    store.append('human-later', {'text':'另一句以后才说的话', 'at':200})
    store.sync_shared_events([{'id':'update','kind':'understanding_update','source':'main_cognition',
                              'at':150,'text':'对已经发生事情的更新'}])
    material = store.material_view(100, knowledge_cutoff_ms=160)
    assert [row['text'] for row in material['events']] == ['对已经发生事情的更新']
    assert not store.material_view(100, knowledge_cutoff_ms=140)['events']


@pytest.mark.parametrize('parse', [_persona_to_model_response, _to_model_response])
def test_cognition_can_update_without_speech(parse):
    response = parse({'mode':'ignore','reply':'','response_plan':{'mode':'ignore'}, 'scene_updates':[UPDATE]}, 'fake')
    assert not response.text
    assert response.scene_updates == (UPDATE,)
    assert replace(response, next_jev_note='').scene_updates == (UPDATE,)


def test_malformed_update_does_not_turn_guess_into_fact():
    assert not parse_scene_updates([{'scope':'大家','understanding':'他们在叫我'}])
    assert not parse_scene_updates([{**UPDATE, 'basis': '依据' * 200}])
    assert not ModelResponse(model='fake', scene_updates='invalid').scene_updates


def test_provenance_is_small_and_input_references_remap_after_merge():
    support = support_for_main({'lines':[{'i':1,'evidence_ref':'I1','short_voice_hint':'同句重复拼接，不增加证据'}],
        'evidence':[{'ref':'I1','voice':{'basis':'无可靠原声命中','score':0.99},
                     'candidates':[{'p':'lux','score':1.0}]}]})
    assert '0.99' not in str(support) and 'lux' not in str(support)
    text = entry_assessments([{'input_ids':['b'],'interaction':'uncertain','decision_reason':'I1指向不清',
                              'entry_support':support}], ['a','b'])
    assert '第2条补充证据：无可靠原声命中' in text
    assert '本轮第2条' in text and '第1条补充' not in text


def test_typed_input_does_not_gain_irrelevant_missing_voice_warning():
    assert not support_for_main({'lines':[{'i':1,'source':'web_text','evidence_ref':'I1'}],
        'evidence':[{'ref':'I1','voice':{'basis':'没有声纹'}}]})


def test_main_process_publishes_silent_update_before_independent_writers(tmp_path):
    from jshi.models import ResponsePlan
    from jshi.models.prompt import build_user
    from tests.test_write_zone_split import runtime, ReplyModel, WriteModel

    class Revising(ReplyModel):
        def generate(self, request):
            return ModelResponse(model=self.name, response_plan=ResponsePlan(mode='ignore'),
                scene_updates=[{**UPDATE,'scope':'P1与P2的设备讨论'}])

    writer=WriteModel()
    process,_=runtime(tmp_path,Revising(),writer)
    result=process.experience('stone','两人继续彼此讨论',object_ref='user',defer_write=True,
        code_restore=lambda text: text.replace('P1','甲').replace('P2','乙'))
    assert not result.response_plan.verbal_text()
    event,=process.pending_scene.shared_events('stone')
    assert event['update']['scope']=='甲与乙的设备讨论'
    jev=JEVSceneStore(tmp_path/'jev.json')
    assert jev.sync_shared_events(process.pending_scene.shared_events('stone'))==1
    assert process.take_deferred_write()()
    assert '主认知认识更新·非人物原话' in build_user(writer.last_request)
    assert not process.pending_scene.snapshot('stone')
    assert process.pending_scene.shared_events('stone')==(event,)
    assert jev.sync_shared_events(process.pending_scene.shared_events('stone'))==0


def test_main_input_reports_jev_attribution_without_false_unknown_name(tmp_path):
    from jshi.core.envelope import InputEnvelope, InputPart, SceneUtterance, SpeakerEvidence
    from jshi.models.prompt import build_user
    from tests.test_write_zone_split import runtime, ReplyModel
    class Capture(ReplyModel):
        def generate(self, request):
            self.request=request
            return super().generate(request)
    model=Capture();process,_=runtime(tmp_path,model,None)
    who=SpeakerEvidence('track','OBJ-USER','user','recognized','jev_attribution')
    current=SceneUtterance('请继续',who,0,1000,input_id='u1')
    envelope=InputEnvelope('s','audio',(InputPart('text','请继续'),),who,current_utterances=(current,))
    process.experience('stone','请继续',object_ref='user',envelope=envelope)
    text=build_user(model.request)
    assert '人物：确定；依据：JEV入口人物判断，证据与缺口见入口材料' in text
    assert '依据：入口未确认姓名' not in text


def test_update_arriving_during_entry_rejudges_with_new_evidence(process):
    import asyncio
    from time import time
    from jshi.app.voice import VoiceConversation
    from jshi.core.envelope import SceneUtterance, SpeakerEvidence
    from jshi.voice.jev import VoiceJEV
    from tests.test_voice import FakeCloud
    from tests.test_jev_split_modes import Model, entry
    people=[{'i':1,'person':'unknown','certainty':'unknown','why':'人物证据不足'}]

    def old_judgment(request):
        # Main corrects its understanding while entry is already in flight.
        process.pending_scene.append_updates('stone','main-correction',[UPDATE])
        return entry(items=people)

    def new_judgment(request):
        assert '主认知认识更新·非人物原话' in request.input_text
        assert UPDATE['basis'] in request.input_text
        return entry('background','keep',items=people,interaction='none')

    model=Model([old_judgment,new_judgment]);sent=[]
    async def scenario():
        async def send(message):sent.append(message)
        voice=VoiceConversation(process,'stone',FakeCloud(),send,
            jev=VoiceJEV(model,mode='split',fallback_models=[]))
        try:
            speaker=SpeakerEvidence('A')
            utterance=SceneUtterance('你会调吗',speaker,0,1000,input_id='a',received_at_ms=time()*1000)
            _,record,_=await voice._judge((utterance,),speaker,False)
            assert record['route']=='background'
            assert 'generation' in record['stale_retry_of']
            assert len([r for r in model.requests if r.purpose=='voice_jev_entry'])==2
            assert not any(r.purpose=='voice_jev_prepare' for r in model.requests)
            assert not any(m.get('type')=='reply' for m in sent)
        finally:await voice.close()
    asyncio.run(scenario())
