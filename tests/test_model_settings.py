import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from jshi.app.model_settings import ModelSettings, install_model_settings
from jshi.models.base import EchoModel, ModelResponse
from jshi.models.routing import ProviderFailure, ProviderModel, RoutedModel


def process(root):
    def port(): return SimpleNamespace(_skill=SimpleNamespace(_model=EchoModel()))
    return SimpleNamespace(repository=SimpleNamespace(path=root/'subject.sqlite3'),cognition=port(),write_zone=port())


def services():
    return {'gemini':{'label':'G','protocol':'gemini','endpoint':'https://example.test/v1beta','api_key':'secret'},
            'deepseek':{'label':'D','protocol':'chat','thinking_supported':True,'paid':True,'endpoint':'https://example.test/v1','api_key':'secret'}}


def test_hot_save_persists_without_exposing_secrets(tmp_path):
    p=process(tmp_path);settings=ModelSettings(tmp_path,p,None)
    data={'services':services(),'name':'cognition','profile':{'effort':'low','choices':[{'service':'gemini','model':'gemini-3.6-flash'},{'service':'deepseek','model':'deepseek-v4-flash'}]}}
    result=settings.save(data)
    assert 'secret' not in json.dumps(result)
    assert p.cognition._skill._model.name=='gemini-3.6-flash'
    next_process=process(tmp_path)
    restored=ModelSettings(tmp_path,next_process,None)
    assert restored.public()['rows']['cognition']['current']=='gemini-3.6-flash'
    before=settings.path.read_bytes()
    data['profile']['choices'].reverse()
    with pytest.raises(ValueError,match='DeepSeek'): settings.save(data)
    assert settings.path.read_bytes()==before
    assert p.cognition._skill._model.name=='gemini-3.6-flash'


def test_gemini_preserves_system_user_and_filters_thoughts():
    model=ProviderModel(services()['gemini'],'gemini-3.6-flash','low')
    request=model._payload([{'role':'system','content':'system rules'},{'role':'user','content':[
        {'type':'text','text':'question'},{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,YQ=='}}]}])
    body=json.loads(request.data)
    assert body['systemInstruction']['parts']==[{'text':'system rules'}]
    assert body['contents'][0]['parts'][1]['inlineData']=={'mimeType':'image/jpeg','data':'YQ=='}
    assert body['generationConfig']['thinkingConfig']=={'thinkingLevel':'low'}
    assert request.get_header('User-agent').startswith('JShi/')
    assert model._text({'candidates':[{'content':{'parts':[{'text':'hidden','thought':True},{'text':'answer'}]}}]})=='answer'


def test_generic_provider_does_not_receive_deepseek_thinking_fields():
    s={**services()['deepseek'],'thinking_supported':False}
    body=json.loads(ProviderModel(s,'free-model')._payload([{'role':'user','content':'OK'}]).data)
    assert 'thinking' not in body and 'reasoning_effort' not in body


@pytest.mark.parametrize('effort,enabled,budget',[('off',False,None),('minimal',False,None),('low',True,512),('high',True,8192)])
def test_qwen_controls_thinking_explicitly(effort,enabled,budget):
    service={**services()['deepseek'],'thinking_style':'qwen'}
    model=ProviderModel(service,'qwen3.5-flash',effort)
    body=json.loads(model._payload([{'role':'user','content':'你好'}]).data)
    assert body['enable_thinking'] is enabled
    assert body.get('thinking_budget')==budget
    assert 'thinking' not in body and 'reasoning_effort' not in body


def test_fallback_and_paid_notice_before_paid_call():
    notices=[]
    class First:
        name='free';service={}
        def generate(self,request):raise ProviderFailure(429,True)
    class Last:
        name='deepseek';service={'paid':True,'label':'Paid'}
        def generate(self,request):
            assert notices==['deepseek']
            return ModelResponse(text='answer',model=self.name)
    router=RoutedModel(First(),on_paid=notices.append);router.replace([First(),Last()])
    assert router.generate(None).text=='answer'
    assert router.last['fallback']
    assert router.last['degraded'] is True
    assert router.last['fallback_reason'] == '429'
    assert router.last['model'] == 'deepseek'
    assert router.last['failed'] == [{'model': 'free', 'reason': '429'}]


def test_timeout_fallback_records_degraded_success():
    class First:
        name='preview';service={}
        def chat(self, messages, max_tokens=None):
            raise ProviderFailure(408, True, 'G · preview', '请求超时（7s）')
    class Lite:
        name='gemini-3.1-flash-lite';service={}
        def chat(self, messages, max_tokens=None):
            return ModelResponse(text='{"mode":"wait"}', model=self.name)
    router = RoutedModel(First()); router.replace([First(), Lite()])
    assert router.chat([{'role':'user','content':'hi'}]).text == '{"mode":"wait"}'
    assert router.last['degraded'] is True
    assert router.last['fallback'] is True
    assert router.last['fallback_reason'] == 'timeout'
    assert router.last['model'] == 'gemini-3.1-flash-lite'
    assert router.last['failed'][0] == {'model': 'preview', 'reason': 'timeout'}


def test_clean_success_is_not_degraded():
    class Only:
        name='gemini-3-flash-preview';service={'label':'G'}
        def chat(self, messages, max_tokens=None):
            return ModelResponse(text='ok', model=self.name)
    router = RoutedModel(Only())
    assert router.chat([{'role':'user','content':'hi'}]).text == 'ok'
    assert router.last['degraded'] is False
    assert router.last['fallback'] is False
    assert router.last['fallback_reason'] == ''
    assert router.last['failed'] == []


def test_hot_chain_gives_lite_degraded_timeout_when_not_first(tmp_path):
    settings = ModelSettings(tmp_path, process(tmp_path), None)
    settings.services['gemini'] = services()['gemini']
    profile = {
        'effort': 'low',
        'choices': [
            {'service': 'gemini', 'model': 'gemini-3-flash-preview'},
            {'service': 'gemini', 'model': 'gemini-3.1-flash-lite'},
        ],
    }
    settings.save({'name': 'cognition', 'profile': profile})
    chain = settings.routes['cognition'].chain
    assert chain[0].name == 'gemini-3-flash-preview'
    assert chain[0].timeout == ProviderModel.DEFAULT_TIMEOUT_S
    assert chain[1].name == 'gemini-3.1-flash-lite'
    from jshi.app.model_settings import DEGRADED_TIMEOUT_S
    assert chain[1].timeout == DEGRADED_TIMEOUT_S


def test_hot_chain_gives_qwen_degraded_timeout_after_lite_before_deepseek(tmp_path):
    """Qwen is slow fallback only: after lite, before DeepSeek; not hot-first."""
    settings = ModelSettings(tmp_path, process(tmp_path), None)
    settings.services['gemini'] = services()['gemini']
    settings.services['deepseek'] = services()['deepseek']
    settings.services['qwen'] = {
        'label': 'Q', 'protocol': 'chat', 'thinking_style': 'qwen', 'paid': True,
        'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'api_key': 'secret',
    }
    profile = {
        'effort': 'low',
        'choices': [
            {'service': 'gemini', 'model': 'gemini-3-flash-preview'},
            {'service': 'gemini', 'model': 'gemini-3.1-flash-lite'},
            {'service': 'qwen', 'model': 'qwen3.5-flash'},
            {'service': 'deepseek', 'model': 'deepseek-v4-flash'},
        ],
    }
    settings.save({'name': 'cognition', 'profile': profile})
    chain = settings.routes['cognition'].chain
    from jshi.app.model_settings import DEGRADED_TIMEOUT_S, SLOW_FALLBACK_MODELS
    assert [m.name for m in chain] == [
        'gemini-3-flash-preview', 'gemini-3.1-flash-lite', 'qwen3.5-flash', 'deepseek-v4-flash',
    ]
    assert chain[0].timeout == ProviderModel.DEFAULT_TIMEOUT_S
    assert 'qwen3.5-flash' in SLOW_FALLBACK_MODELS
    assert chain[1].timeout == DEGRADED_TIMEOUT_S
    assert chain[2].timeout == DEGRADED_TIMEOUT_S
    assert chain[2].service.get('thinking_style') == 'qwen'
    # DeepSeek last keeps default hot timeout (paid last resort, not slow-fallback set).
    assert chain[3].timeout == ProviderModel.DEFAULT_TIMEOUT_S


def test_no_fallback_after_stream_has_started():
    class First:
        name='free';service={}
        def generate_stream(self,request):
            yield 'partial'
            raise ProviderFailure(503,True)
    class Last:
        name='unused';service={}
        def generate_stream(self,request):raise AssertionError('must not splice outputs')
    router=RoutedModel(First());router.replace([First(),Last()])
    stream=router.generate_stream(None)
    assert next(stream)=='partial'
    with pytest.raises(ProviderFailure): next(stream)


def test_live_change_keeps_started_request_on_old_chain():
    router=None
    class Old:
        name='old';service={}
        def generate(self,request):
            router.replace([New()])
            raise ProviderFailure(429,True)
    class Tail:
        name='tail';service={}
        def generate(self,request):return ModelResponse(text='old tail',model=self.name)
    class New:
        name='new';service={}
        def generate(self,request):return ModelResponse(text='new',model=self.name)
    router=RoutedModel(Old());router.replace([Old(),Tail()])
    assert router.generate(None).text=='old tail'
    assert router.generate(None).text=='new'


def test_settings_routes_require_origin_and_never_test_on_save(tmp_path):
    async def run():
        app=web.Application();settings=install_model_settings(app,process(tmp_path),None)
        async with TestClient(TestServer(app)) as client:
            payload={'services':services(),'name':'cognition','profile':{'effort':'low','choices':[{'service':'gemini','model':'gemini-3.6-flash'}]}}
            assert (await client.post('/model-settings',json=payload)).status==403
            origin=str(client.make_url('')).rstrip('/')
            response=await client.post('/model-settings',json=payload,headers={'Origin':origin})
            assert response.status==200
            assert 'secret' not in await response.text()
            assert (await client.get('/model-settings',headers={'Sec-Fetch-Site':'cross-site'})).status==403
    asyncio.run(run())


def test_global_save_and_restart_apply_to_all_purposes(tmp_path):
    p=process(tmp_path);settings=ModelSettings(tmp_path,p,None)
    profile={'effort':'low','choices':[{'service':'gemini','model':'gemini-3.6-flash'}]}
    settings.save({'services':services(),'name':'all','profile':profile})
    assert all(route.name=='gemini-3.6-flash' for route in settings.routes.values())
    next_settings=ModelSettings(tmp_path,process(tmp_path),None)
    assert next_settings.public()['global_profile']==profile
    assert all(route.name=='gemini-3.6-flash' for route in next_settings.routes.values())
    settings.save({'name':'all','profile':{'choices':[]}})
    assert all(route.chain==settings.originals[name] for name,route in settings.routes.items())


def test_one_save_sets_qwen_key_and_single_global_model(tmp_path):
    settings=ModelSettings(tmp_path,process(tmp_path),None)
    settings.save({'name':'all','services':{'qwen':{'endpoint':'https://dashscope.aliyuncs.com/compatible-mode/v1','api_key':'secret'}},
        'profile':{'effort':'low','choices':[{'service':'qwen','model':'qwen3.5-flash'}]}})
    assert all(len(route.chain)==1 and route.name=='qwen3.5-flash' for route in settings.routes.values())
    assert 'secret' not in json.dumps(settings.public())


def test_qwen_free_tier_error_is_specific_and_safe(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    def fail(request,timeout):raise HTTPError(request.full_url,403,'error',{},BytesIO(b'{"error":{"code":"AllocationQuota.FreeTierOnly","message":"quota secret"}}'))
    monkeypatch.setattr('jshi.models.routing.urlopen',fail)
    model=ProviderModel(services()['deepseek'],'qwen3.5-flash')
    with pytest.raises(ProviderFailure) as error:model.chat([{'role':'user','content':'OK'}])
    assert 'allocationquota.freetieronly' in str(error.value)
    assert 'secret' not in str(error.value)


def test_global_low_keeps_entry_jev_non_thinking_across_restart(tmp_path):
    from jshi.voice.jev import VoiceJEV
    settings=ModelSettings(tmp_path,process(tmp_path),VoiceJEV(EchoModel()))
    settings.services['deepseek']=services()['deepseek']
    profile={'effort':'low','choices':[{'service':'deepseek','model':'deepseek-v4-flash'}]}
    settings.save({'name':'all','profile':profile})
    assert settings.routes['cognition'].chain[0].effort=='low'
    assert settings.routes['voice_jev'].chain[0].effort=='off'
    restored=ModelSettings(tmp_path,process(tmp_path),VoiceJEV(EchoModel()))
    assert restored.routes['voice_jev'].chain[0].effort=='off'


@pytest.mark.parametrize('status,body,retryable',[(403,b'error code: 1010',True),(403,b'permission denied',False),(401,b'invalid key',False),(429,b'quota exceeded',True)])
def test_http_reason_classification_does_not_leak_body(monkeypatch,status,body,retryable):
    from io import BytesIO
    from urllib.error import HTTPError
    def fail(request,timeout):raise HTTPError(request.full_url,status,'error',{},BytesIO(body))
    monkeypatch.setattr('jshi.models.routing.urlopen',fail)
    model=ProviderModel(services()['gemini'],'gemini-3.6-flash')
    with pytest.raises(ProviderFailure) as error:model.chat([{'role':'user','content':'OK'}])
    assert error.value.retryable==retryable
    assert 'secret' not in str(error.value)
    assert 'gemini-3.6-flash' in str(error.value)


def test_model_status_pairs_each_fallback_attempt_and_stream_cancellation():
    events = []
    class First:
        name = 'first'
        def chat(self, messages, max_tokens=None):
            raise ProviderFailure(429, True)
    class Last:
        name = 'last'
        def chat(self, messages, max_tokens=None):
            return ModelResponse(text='ok', model=self.name)
        def generate_stream(self, request):
            yield 'one'
            yield 'two'
    router = RoutedModel(First(), on_status=events.append)
    router.replace([First(), Last()])
    assert router.chat([]).text == 'ok'
    assert [e['state'] for e in events] == ['calling', 'failed', 'calling', 'completed']
    assert events[0]['call_id'] == events[1]['call_id']
    assert events[2]['call_id'] == events[3]['call_id']
    assert events[0]['call_id'] != events[2]['call_id']
    router.replace([Last()])
    stream = router.generate_stream(None)
    assert next(stream) == 'one'
    stream.close()
    assert [e['state'] for e in events[-2:]] == ['calling', 'cancelled']
    assert events[-1]['elapsed_ms'] >= 0


def test_model_status_callback_failure_does_not_break_response():
    class Model:
        name = 'local-test'
        def generate(self, request):
            return ModelResponse(text='ok', model=self.name)
    def broken(event):
        raise RuntimeError('observer failed')
    assert RoutedModel(Model(), on_status=broken).generate(None).text == 'ok'
