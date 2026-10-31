import json
from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV
from tests.test_jev_split_modes import entry


def test_malformed_json_retries_once_without_repairing_text():
    valid=entry(route='background',control='keep',interaction='none',process_refs=[],pending_refs=[])
    class Model:
        calls=0
        def generate(self,request):
            self.calls+=1
            return ModelResponse(text='{"broken":' if self.calls==1 else json.dumps(valid),model='fake')
    model=Model();gate=VoiceJEV(model,mode='split',fallback_models=[])
    result=gate.decide_batch('stone',[{'n':1,'text':'电视讲话','who':'P1'}],[],{})
    assert model.calls==2 and not result.model_error and result.route=='background'
    assert result.stage_records[0]['status']=='retried'
    assert result.stage_records[1]['attempt']==2


def test_bad_schema_is_not_retried_and_bad_json_stops_at_two_calls():
    for raw,count in [('{}',1),('{',2)]:
        class Model:
            calls=0
            def generate(self,request):
                self.calls+=1;return ModelResponse(text=raw,model='fake')
        model=Model()
        result=VoiceJEV(model,mode='split',fallback_models=[]).decide_batch(
            'stone',[{'n':1,'text':'背景','who':'P1'}],[],{})
        assert result.model_error and model.calls==count
