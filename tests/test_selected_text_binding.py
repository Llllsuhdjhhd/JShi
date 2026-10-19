import json
from jshi.voice.jev import VoiceJEV
from jshi.models import ModelResponse

class Model:
    def __init__(self, control='keep', fail=False):
        self.control, self.fail = control, fail
    def generate(self, request):
        if self.fail:
            raise RuntimeError('offline')
        payload=json.loads(request.input_text)
        assert payload['input']['lines'][0]['binding']['to_jiangshi']=='yes'
        return ModelResponse(model='fake', text=json.dumps({'items':[{'i':1,'person':'unknown','certainty':'unknown','why':'未点名'}], 'level':3,'recall_memory':False,'control':self.control,'route':'main','reply':''}))


def test_selected_text_binding_survives_wrong_model_identity():
    row={'n':1,'text':'哈喽','who':'P1','source':'web_text','speaker':{'method':'user_selected'}}
    for model in (Model(), Model(fail=True), None):
        result=VoiceJEV(model).decide_batch('stone',[row],[],{})
        assert result.action=='respond' and result.control=='redirect' and result.judged
        item=result.items[0]
        assert (item.speaker_pick,item.speaker_level,item.to_jiangshi)==('P1','确定','yes')


def test_selected_text_keeps_semantic_stop_during_playback():
    row={'n':1,'text':'别说了','who':'P1','source':'web_text','speaker':{'method':'user_selected'}}
    result=VoiceJEV(Model('stop')).decide_batch('stone',[row],[],{'state':'playing'})
    assert result.control=='stop' and result.action=='stop'
    assert result.items[0].speaker_pick=='P1'


def test_unbound_text_does_not_get_program_identity():
    result=VoiceJEV().decide_batch('stone',[{'n':1,'text':'你好','who':'P1','source':'web_text'}],[],{})
    assert result.items[0].speaker_level!='确定'
