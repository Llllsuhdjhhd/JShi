import json
from dataclasses import replace

import pytest

from jshi.core.prompt_profile import PromptProfile, select_profile
from jshi.memory.portrait_views import PortraitViews, CAPS
from jshi.assembly.sources import MemorySource
from jshi.assembly.sources import PersonPortraitSource
from jshi.assembly.port import AssemblyContext, AssemblySpeaker, AssemblyFragment
from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV
from tests.test_voice import process


def test_profile_is_applied_before_memory_and_tool_loading(process):
    class Memory:
        def recall(self, *args, **kwargs):
            raise AssertionError('历史召回不应运行')
    memory = MemorySource(process.repository, Memory())
    assert memory.load(AssemblyContext('stone','谢谢',AssemblySpeaker('lux-id','lux'),recall_enabled=False)).fragments == ()
    process.assembler.sources = (*process.assembler.sources, memory)
    process.tool_service.list_tool_related_entries = lambda *a, **k: pytest.fail('无工具档不应装载旧结果')
    current = process.assemble_current_state('stone','谢谢',object_id='lux-id',prompt_profile=PromptProfile(3,recall_memory=False))
    assert current.prompt_level == 3 and not current.recall_memory
    assert not current.tool_input
    assert all(not r.error for r in current.source_report)
    assert all(r.elapsed_ms >= 0 for r in current.source_report)


def test_memory_switch_keeps_scene_and_person_reference(process):
    process.style_packs.set('stone','smith')
    process.zone_store.apply_edit('stone',(),append_blocks=('刚才已约定下午去店里。',))
    current=replace(process.assemble_current_state('stone','谢谢'),prompt_level=3,recall_memory=False,
        fragments=(AssemblyFragment('memory','m','旧旅游细节','event',object_id='lux-id'),
                   AssemblyFragment('person_portrait','p','自述创建匠石；身份关系按确认来源使用。','portrait',object_id='lux-id')))
    text=process._persona_user_text(current,boot=False)
    assert '刚才已约定' in text and '自述创建匠石' in text
    assert '旧旅游细节' not in text
    assert select_profile(['a'],[{'input_ids':['a'],'main_prompt_hint':{'level':3,'recall_memory':False}}]).recall_memory is False
    calls=[{'input_ids':['a'],'main_prompt_hint':{'level':3,'recall_memory':False}},
           {'input_ids':['b'],'main_prompt_hint':{'level':2,'recall_memory':True}}]
    assert select_profile(['a','b'],calls)==PromptProfile(2,recall_memory=True)


def test_three_portrait_views_are_validated_persisted_and_invalidated(tmp_path):
    raw={'subject_id':'stone','object_id':'lux-id','levels':{'L1':'旧经历。'*400}}
    store=PortraitViews(tmp_path/'views.json')
    class Model:
        def complete_json(self,purpose,messages):
            assert purpose=='person_portrait'
            return {'levels':{'L1':'自述创建匠石。近期常做语音测试，归属需证据。',
                              'L2':'自述创建匠石。归属需证据。','L3':'自述创建匠石。'}}
    preview,ready=store.get(raw)
    assert not ready and all(len(preview['levels'][f'L{i}'])<=cap for i,cap in enumerate(CAPS,1))
    store.refresh(raw,Model())
    saved,ready=PortraitViews(tmp_path/'views.json').get(raw)
    assert ready and saved['levels']['L3']=='自述创建匠石。'
    assert not store.get({**raw,'levels':{'L1':'新纠正。'*400}})[1]
    class Bad:
        def complete_json(self,*args): return {'levels':{'L1':'字'*801}}
    with pytest.raises(ValueError):
        store.refresh({**raw,'levels':{'L1':'新纠正。'*400}},Bad())
    assert store.get(raw)[1]


def test_person_level_is_selected_by_name_even_if_full_view_fits_middle_cap(process):
    levels={'L1':'完整肖像。'*30,'L2':'中档人物精华。','L3':'短档精华。'}
    class Memory:
        def portrait(self,*args): return {'subject_id':'stone','object_id':'lux-id','levels':levels}
    source=PersonPortraitSource(Memory())
    ctx=AssemblyContext('stone','你好',AssemblySpeaker('lux-id','lux',status='confirmed'),portrait_budget_chars=400)
    fragment=source.load(ctx).fragments[0]
    assert fragment.content==levels['L2']
    assert levels['L2'] in process._person_portrait_lines((fragment,),budget_chars=400)[0]
    assert levels['L1'] in process._person_portrait_lines((fragment,),budget_chars=800)[0]


def test_low_voice_candidate_can_be_finally_confirmed_and_select_memory_independently():
    class Gate:
        def generate(self,request):
            p=json.loads(request.input_text)
            assert p['input']['lines'][0]['track']=='7'
            assert p['input']['unwritten'][0]['p']=='P2'
            return ModelResponse(model='gate',text=json.dumps({'level':3,'recall_memory':False,'control':'pass',
                'items':[{'i':1,'person':'P2','certainty':'confirmed','to':'maybe','keep':'related',
                          'why':'候选持续领先，近期P2已确认，当前共同活动与接续综合支持'}]}))
    result=VoiceJEV(Gate()).decide_batch('stone',[
        {'n':1,'who':'P22（待定声音3）','track':'7','text':'看小狗在飞。',
         'voice_evidence':{'pick':'P22','strength':'weak'},'candidates':[{'who':'P2','score':.153}]}],
        [{'p':'P2','text':'不美好不简单。','at':1000}],{'jev_scene':'两人在家共同看东西，近期已确认P2。'})
    assert result.judged and result.items[0].speaker_pick=='P2'
    assert result.items[0].speaker_level=='确定'
    assert result.main_prompt_hint['level']==3 and result.main_prompt_hint['recall_memory'] is False


def test_temporary_voice_group_is_not_presented_as_named_identity():
    voice={'pick':'P22','strength':'weak','basis':'临时声音连续性；只认出同一个声音'}
    class Gate:
        def generate(self,request):
            p=json.loads(request.input_text)['input']['lines'][0]
            assert p['identity']=='unresolved'
            assert p['continuity']['scope']=='continuity_only'
            assert p['voice']['pick']==''
            assert p['voice_ranking']['first']=='P2'
            assert p['recent_confirmed']=={'p':'P2','gap_ms':3131,'same_track':True}
            return ModelResponse(model='gate',text='{"items":[{"i":1,"person":"P2","certainty":"tentative","to":"no","keep":"related","why":"候选领先且近期确认接续"}],"level":3,"recall_memory":false,"control":"pass"}')
    result=VoiceJEV(Gate()).decide_batch('stone',[{'n':1,'who':'P22','track':'7','at':4131,
        'text':'看小狗在飞','voice_evidence':voice,'candidates':[{'who':'P2','score':.153}]}],
        [{'p':'P2','at':1000,'track':'7','status':'recognized','text':'前一句'}],{})
    assert result.items[0].speaker_pick=='P2'
    assert 'scope' not in voice  # Transport annotations must not rewrite the original evidence.
