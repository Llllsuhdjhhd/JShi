"""Local model settings. No credentials in public responses or tracked files."""
import asyncio
import json
import os
import re
from pathlib import Path
from threading import RLock
from time import monotonic
from urllib.parse import urlsplit

from jshi.core.skillconfig import SkillConfigStore
from jshi.models.base import ModelResponse
from jshi.models.routing import ProviderModel, RoutedModel

LABELS = {'cognition':'主认知／回应','voice_jev':'JEV入口判断','write_zone':'后台写场',
          'introspection':'自省','vision':'视觉理解','tool_plan':'工具规划','tool_wrap':'工具结果整理'}
MODELS = {'deepseek':['deepseek-v4-flash','deepseek-v4-pro'],
          'qwen':['qwen3.5-flash','qwen3.5-flash-2026-02-23'],
          # Hot path prefers gemini-3-flash-preview (empirically ~1s); flash-lite is slow/fallback only.
          'gemini':['gemini-3-flash-preview','gemini-3.6-flash','gemini-3.8-flash','gemini-3.5-flash','gemini-3.1-flash-lite'],
          'openrouter':['nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free'],
          'cloudflare':['@cf/meta/llama-4-scout-17b-16e-instruct','@cf/mistralai/mistral-small-3.1-24b-instruct']}
# When preferred hot models time out / 429, fall through to these with a longer
# budget so the answer is a degraded success instead of a null prediction.
# Slow/degraded fallbacks only (never preferred hot). Order when chained:
# preview-first → flash-lite → qwen3.5-flash → DeepSeek last.
SLOW_FALLBACK_MODELS = frozenset({
    'gemini-3.1-flash-lite',
    'gemini-3.5-flash-lite',
    'qwen3.5-flash',
    'qwen3.5-flash-2026-02-23',
})
DEGRADED_TIMEOUT_S = 25.0


class ModelSettings:
    def __init__(self, root, process, jev, on_paid=None):
        self.path = Path(root)/'model_settings.local.json'
        self.lock = RLock()
        self.routes = {}
        for name, port in [('cognition',process.cognition),('write_zone',process.write_zone)]:
            if hasattr(port,'_skill'):
                route = RoutedModel(port._skill._model)
                port._skill._model = route
                self.routes[name] = route
        if jev is not None and jev.model is not None:
            self.routes['voice_jev'] = RoutedModel(jev.model)
            jev.model = self.routes['voice_jev']
        tools=getattr(process,'tool_service',None)
        for name,skill in [('tool_plan',getattr(getattr(tools,'planner',None),'skill',None)),
                           ('tool_wrap',getattr(tools,'wrap_skill',None))]:
            if skill is not None and hasattr(skill,'_model'):
                self.routes[name]=RoutedModel(skill._model)
                skill._model=self.routes[name]
        reflection = getattr(process,'reflection_executor',None)
        if reflection is not None and getattr(reflection,'model',None) is not None:
            self.routes['introspection'] = RoutedModel(reflection.model)
            reflection.model = self.routes['introspection']
        visual = getattr(process,'vision',None)
        if visual and hasattr(visual.model,'transport'):
            original = visual.model.transport
            class OriginalVision:
                name = visual.config.model
                def chat(self, messages, max_tokens=None):
                    payload={'model':self.name,'messages':messages,'max_tokens':max_tokens or 1200,'stream':False}
                    result=original(payload)
                    return ModelResponse(text=result['choices'][0]['message']['content'],model=self.name)
            route=RoutedModel(OriginalVision())
            self.routes['vision']=route
            def send(payload):
                result=route.chat(payload['messages'],payload.get('max_tokens'))
                return {'choices':[{'message':{'content':result.text}}]}
            visual.model.transport=send
        self.originals = {name:route.chain for name,route in self.routes.items()}
        for name, route in self.routes.items():
            route.on_paid = lambda model, name=name: on_paid(LABELS[name],model) if on_paid else None
        self.services = {'deepseek':{'label':'DeepSeek（付费兜底）','paid':True,'protocol':'chat','thinking_supported':True,
            'endpoint':os.getenv('JSHI_API_DEEPSEEK_ENDPOINT') or os.getenv('JSHI_MODEL_ENDPOINT',''),
            'api_key':os.getenv('JSHI_API_DEEPSEEK_API_KEY') or os.getenv('JSHI_MODEL_API_KEY','')},
          'gemini':{'label':'Gemini网关','protocol':'gemini','endpoint':'','api_key':''},
          'qwen':{'label':'阿里云百炼 Qwen（可计费）','protocol':'chat','thinking_style':'qwen','paid':True,
              # Prefer explicit JSHI_* ; else DashScope env from dashscope.env (never hardcode key).
              'endpoint':(os.getenv('JSHI_API_QWEN_ENDPOINT') or os.getenv('DASHSCOPE_BASE_URL')
                          or 'https://dashscope.aliyuncs.com/compatible-mode/v1'),
              'api_key':os.getenv('JSHI_API_QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY','')},
          'cloudflare':{'label':'Cloudflare Workers AI','protocol':'chat','endpoint':'','api_key':''},
          'openrouter':{'label':'OpenRouter免费','protocol':'chat','endpoint':'https://openrouter.ai/api/v1','api_key':''}}
        self.profiles = {}
        self.global_profile = None
        if self.path.exists():
            saved=json.loads(self.path.read_text(encoding='utf8'))
            self.services.update(saved.get('services',{}))
            self.profiles=saved.get('profiles',{})
            self.global_profile=saved.get('global_profile')
            if self.global_profile:
                self.profiles={name:self.global_profile for name in self.routes}
            for name,profile in self.profiles.items():
                if name in self.routes: self.routes[name].replace(self._chain(profile,self.services,name))

    def public(self):
        with self.lock:
            store=SkillConfigStore()
            from jshi.app.cli import _skills_config_path
            path=_skills_config_path()
            if path: store.load_file(path)
            rows={}
            for name,route in self.routes.items():
                base=store.profile(name)
                effort='off' if base.thinking=='disabled' else base.reasoning_effort
                rows[name]={'label':LABELS[name],'current':route.name,'last':dict(route.last),
                    'profile':self.profiles.get(name,{'choices':[], 'effort':effort})}
            services={k:{field:v for field,v in s.items() if field!='api_key'}|{'has_key':bool(s.get('api_key')),'models':MODELS.get(k,[])} for k,s in self.services.items()}
            return {'rows':rows,'services':services,'global_profile':self.global_profile}

    def _chain(self, profile, services, purpose=None):
        choices=profile.get('choices',[])
        if not isinstance(choices,list) or not 1<=len(choices)<=4: raise ValueError('请选择1至4个模型')
        effort=profile.get('effort','low')
        if effort not in {'off','minimal','low','high'}: raise ValueError('思考强度无效')
        chain=[]
        for index,choice in enumerate(choices):
            service=services.get(choice.get('service'))
            model=str(choice.get('model','')).strip()
            if not service or not service.get('api_key') or not service.get('endpoint'): raise ValueError('请先配置服务地址和密钥')
            if not model or len(model)>160 or not re.fullmatch(r'[\w@/.:+\-]+',model): raise ValueError('模型名称无效')
            if choice['service']=='deepseek' and index!=len(choices)-1: raise ValueError('付费DeepSeek必须排在最后')
            if choice['service']=='openrouter' and not model.endswith(':free'): raise ValueError('OpenRouter备用只允许选择:free模型')
            if service['protocol']=='gemini' and model.startswith('gemini-3') and (effort=='off' or model=='gemini-3.8-flash' and effort=='minimal'):
                raise ValueError('所选Gemini不支持该思考强度，请选轻量或充分')
            selected='off' if service.get('thinking_supported') and effort=='minimal' else effort
            # Entry judgment used to be non-thinking. Global model choice must
            # not turn it into a slow reasoning call on every utterance.
            if purpose=='voice_jev':
                selected=('low' if model=='gemini-3.8-flash' else 'minimal') if service['protocol']=='gemini' and model.startswith('gemini-3') else 'off'
            # Interactive routes: hard-cap HTTP wait so one slow call cannot push p95 past ~8s.
            # Background write/introspection keep the longer ceiling.
            # Slow fallback models (flash-lite / qwen) in non-first position get a longer
            # degraded budget so timeout/429 on preferred models still yield an answer.
            hot = purpose in {None, 'cognition', 'voice_jev', 'tool_plan', 'tool_wrap', 'vision'}
            if not hot:
                timeout = 45.0
            elif index > 0 and model in SLOW_FALLBACK_MODELS:
                timeout = DEGRADED_TIMEOUT_S
            else:
                timeout = ProviderModel.DEFAULT_TIMEOUT_S
            chain.append(ProviderModel(service, model, selected, timeout=timeout))
        return chain

    def save(self, data):
        with self.lock:
            services={k:dict(v) for k,v in self.services.items()}
            for name,raw in data.get('services',{}).items():
                if name not in MODELS: raise ValueError('未知服务')
                base=services.get(name,{'label':{'gemini':'Gemini网关','openrouter':'OpenRouter免费','cloudflare':'Cloudflare Workers AI'}.get(name,name),
                    'protocol':'gemini' if name=='gemini' else 'chat','thinking_supported':name=='deepseek'})
                endpoint=str(raw.get('endpoint',base.get('endpoint',''))).strip()
                url=urlsplit(endpoint)
                if url.scheme!='https' or not url.netloc or url.username or url.password or url.query or url.fragment: raise ValueError('服务地址须为不含密钥的HTTPS地址')
                services[name]={**base,'endpoint':endpoint,'api_key':raw.get('api_key') or base.get('api_key','')}
            name=data.get('name')
            profiles=dict(self.profiles)
            global_profile=self.global_profile
            chain=None
            if name:
                profile=data.get('profile',{})
                if name=='all':
                    if profile.get('choices'):
                        self._chain(profile,services)
                        global_profile=profile
                        profiles={n:profile for n in self.routes}
                    else:
                        global_profile=None
                        profiles={}
                else:
                    if name not in self.routes: raise ValueError('此用途当前不可配置')
                    if profile.get('choices'): chain=self._chain(profile,services,name);profiles[name]=profile
                    else: profiles.pop(name,None);chain=self.originals[name]
            # Changed credentials also replace affected chains for subsequent calls.
            updates={n:self._chain(p,services,n) for n,p in profiles.items() if n in self.routes}
            if name=='all' and global_profile is None: updates=self.originals
            elif name and chain is not None: updates[name]=chain
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temp=self.path.with_suffix('.tmp')
            temp.write_text(json.dumps({'services':services,'profiles':profiles,'global_profile':global_profile},ensure_ascii=False,indent=2),encoding='utf8')
            temp.replace(self.path)
            self.services,self.profiles=services,profiles
            self.global_profile=global_profile
            for n,models in updates.items(): self.routes[n].replace(models)
            return self.public()

    def test(self, data):
        with self.lock: chain=self._chain(data['profile'],self.services)
        started=monotonic()
        # Only a fixed public sentence. Testing never saves or changes live routing.
        reply=chain[0].chat([{'role':'user','content':'Reply with OK only.'}],max_tokens=1024)
        return {'model':reply.model,'seconds':round(monotonic()-started,2),'text':reply.text[:160]}


def install_model_settings(app, process, jev, on_paid=None):
    from aiohttp import web
    settings=ModelSettings(process.repository.path.parent,process,jev,on_paid)
    def same_origin(request):
        origin=request.headers.get('Origin')
        return origin==f'{request.scheme}://{request.host}' and request.headers.get('Sec-Fetch-Site')!='cross-site'
    async def read(request):
        if request.headers.get('Sec-Fetch-Site')=='cross-site': raise web.HTTPForbidden()
        return web.json_response(settings.public())
    async def change(request):
        if not same_origin(request): raise web.HTTPForbidden()
        try:
            data=await request.json()
            if request.path.endswith('/test'): result=await asyncio.to_thread(settings.test,data)
            else: result=settings.save(data)
            return web.json_response(result)
        except (ValueError,KeyError,TypeError) as exc: return web.json_response({'error':str(exc)},status=400)
        except Exception:
            # Provider/transport errors may contain keys in URLs: don't expose them.
            return web.json_response({'error':'服务未成功返回，请检查连接、额度或模型可用性；当前配置未切换'},status=502)
    app.router.add_get('/model-settings',read)
    app.router.add_post('/model-settings',change)
    app.router.add_post('/model-settings/test',change)
    async def script(request): return web.FileResponse(Path(__file__).with_name('model_settings.js'))
    app.router.add_get('/model_settings.js',script)
    return settings
