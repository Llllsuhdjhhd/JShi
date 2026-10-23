import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV
from jshi.app.voice import VoiceConversation
from jshi.core.envelope import SceneUtterance, SpeakerEvidence
from tests.test_voice import process, FakeCloud


@pytest.mark.parametrize('value,expected', [('complete','complete'),('incomplete','incomplete'),('uncertain','uncertain'),(None,'uncertain'),('bad','uncertain'),([],'uncertain')])
def test_explicit_completeness_and_old_model_compatibility(value, expected):
    class Model:
        def generate(self, request):
            schema = json.loads(request.system_extra.split('JSON Schema：')[-1])
            assert 'utterance_complete' in schema['required']
            assert schema['properties']['utterance_complete']['enum'] == ['complete','incomplete','uncertain']
            data = {'items':[{'i':1,'person':'unknown','certainty':'unknown','why':'声音未定'}],
                    'level':1,'recall_memory':False,'main_image':'omit',
                    'control':'keep','route':'main','reply':''}
            if value is not None:
                data['utterance_complete'] = value
            return ModelResponse(model='fake',text=json.dumps(data))
    result = VoiceJEV(Model()).decide_batch('stone',[{'n':1,'text':'如果明天下雨'}],[],{})
    assert result.utterance_complete == expected
    assert result.control == 'keep'
    assert result.judged


def test_retention_independent_of_control_and_other_speakers(process):
    async def run():
        async def send(m): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            a=SceneUtterance('如果明天下雨',SpeakerEvidence('A',object_id='a'),0,1000,input_id='a')
            b=replace(a,text='好',input_id='b',speaker=SpeakerEvidence('B',object_id='b'))
            c._update_jev_continuation((a,),{'utterance_complete':'incomplete','control':'redirect'})
            assert list(c.jev_pending_inputs) == ['a']
            c._update_jev_continuation((b,),{'utterance_complete':'complete','control':'keep'})
            assert list(c.jev_pending_inputs) == ['a']
            c._update_jev_continuation((a,),{'utterance_complete':'complete','control':'keep'})
            assert not c.jev_pending_inputs
            c._update_jev_continuation((a,),{'control':'keep'})
            assert list(c.jev_pending_inputs) == ['a']
            c._update_jev_continuation((a,),{'utterance_complete':'incomplete','control':'stop'})
            assert not c.jev_pending_inputs
        finally:
            await c.close()
    asyncio.run(run())


def test_incomplete_does_not_override_explicit_stop():
    class Model:
        def generate(self, request):
            return ModelResponse(model='fake',text=json.dumps({
                'items':[{'i':1,'person':'unknown','certainty':'unknown','why':'未知'}],
                'level':1,'recall_memory':False,'main_image':'omit',
                'utterance_complete':'incomplete','control':'stop','route':'main','reply':''}))
    result=VoiceJEV(Model()).decide_batch('stone',[{'n':1,'text':'先别说，我还要'}],[],{'state':'playing'})
    assert result.utterance_complete == 'incomplete'
    assert result.control == 'stop' and result.action == 'stop'


@pytest.mark.parametrize('source', ['voice', 'web_text'])
@pytest.mark.parametrize('state', ['completed', 'playing'])
def test_incomplete_cannot_redirect_even_with_bound_text(source, state):
    class Model:
        def generate(self, request):
            return ModelResponse(model='fake',text=json.dumps({
                'items':[{'i':1,'person':'P1','certainty':'confirmed','why':'人物明确'}],
                'level':3,'recall_memory':False,'main_image':'omit',
                'utterance_complete':'incomplete','control':'redirect',
                'route':'self_reply','reply':'错误抢答','waiting_reply':'我想一下'}))
    row={'n':1,'text':'如果明天','who':'P1','source':source,
         'speaker':{'method':'user_selected' if source=='web_text' else 'voiceprint'}}
    result=VoiceJEV(Model()).decide_batch('stone',[row],[],{'state':state,
        'short_chat':{'allowed':True,'person_code':'P1'}})
    assert result.utterance_complete == 'incomplete'
    assert result.control == 'keep' and result.action == 'ignore'
    assert result.direct_reply == '' and result.waiting_reply == ''
