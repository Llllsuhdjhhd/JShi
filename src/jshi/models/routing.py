"""Provider adapters and per-request snapshots for live model routing."""
from contextlib import contextmanager
from time import monotonic
from uuid import uuid4
import json
import re
from threading import RLock
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request

from jshi.models.http_pool import urlopen

from .base import ModelResponse
from .prompt import build_system, build_user


class ProviderFailure(RuntimeError):
    def __init__(self, status, retryable, context='', reason=''):
        super().__init__(f'{context+"：" if context else ""}模型服务返回HTTP {status}{"；"+reason if reason else ""}')
        self.status = status
        self.retryable = retryable
        self.reason = reason


class ProviderModel:
    # Hot-path default: cap a single HTTP wait so one slow upstream cannot
    # drag cognition / JEV p95 past the interactive budget (~8s). Background
    # routes (write_zone / introspection) pass a higher timeout explicitly.
    DEFAULT_TIMEOUT_S = 7.0

    def __init__(self, service, model, effort='low', max_tokens=None, timeout=None):
        self.service, self.name, self.effort = dict(service), model, effort
        self.max_tokens = max_tokens
        self.timeout = self.DEFAULT_TIMEOUT_S if timeout is None else float(timeout)

    def _payload(self, messages, stream=False, max_tokens=None):
        service = self.service
        if service['protocol'] == 'gemini':
            system, contents = [], []
            for message in messages:
                if message['role'] == 'system':
                    system.append({'text':message['content']})
                    continue
                content = message['content']
                parts = [{'text':content}] if isinstance(content, str) else []
                for item in content if isinstance(content, list) else []:
                    if item['type'] == 'text':
                        parts.append({'text':item['text']})
                    elif item['type'] == 'image_url':
                        url = item['image_url']['url']
                        if not url.startswith('data:'):
                            raise ValueError('Gemini图片需要本地内联数据')
                        head, data = url.split(',', 1)
                        parts.append({'inlineData':{'mimeType':head[5:].split(';')[0], 'data':data}})
                contents.append({'role':'model' if message['role']=='assistant' else 'user','parts':parts})
            config = {}
            if self.name.startswith('gemini-3'):
                if self.effort == 'off':
                    raise ValueError('Gemini 3不支持完全关闭思考，请选最低或轻量')
                if self.name == 'gemini-3.8-flash' and self.effort == 'minimal':
                    raise ValueError('Gemini 3.8 Flash不支持最低思考，请选轻量')
                config['thinkingConfig'] = {'thinkingLevel':self.effort}
            elif self.name.startswith('gemini-2.5'):
                config['thinkingConfig'] = {'thinkingBudget':{'off':0,'minimal':128,'low':1024,'high':-1}[self.effort]}
            if max_tokens or self.max_tokens:
                config['maxOutputTokens'] = max_tokens or self.max_tokens
            body = {'contents':contents,'generationConfig':config}
            if system: body['systemInstruction'] = {'parts':system}
            endpoint = service['endpoint'].rstrip('/') + '/models/' + quote(self.name, safe='-._')
            endpoint += ':streamGenerateContent?alt=sse' if stream else ':generateContent'
            headers = {'x-goog-api-key':service['api_key']}
        else:
            body = {'model':self.name,'messages':messages,'stream':stream}
            if service.get('thinking_style')=='qwen':
                body['enable_thinking']=self.effort not in {'off','minimal'}
                if body['enable_thinking']:
                    body['thinking_budget']=512 if self.effort=='low' else 8192
            elif service.get('thinking_supported'):
                body['thinking'] = {'type':'disabled' if self.effort=='off' else 'enabled'}
                if self.effort!='off': body['reasoning_effort'] = self.effort
            if max_tokens or self.max_tokens: body['max_tokens'] = max_tokens or self.max_tokens
            endpoint = service['endpoint'].rstrip('/')
            if not endpoint.endswith('/chat/completions'): endpoint += '/chat/completions'
            headers = {'Authorization':'Bearer '+service['api_key']}
        return Request(endpoint, data=json.dumps(body).encode('utf8'), headers={**headers,'Content-Type':'application/json',
            'User-Agent':'JShi/1.0 (+Python HTTP client)', 'Accept':'text/event-stream' if stream else 'application/json'})

    def _open(self, request):
        try:
            return urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            detail = exc.read(8192).decode('utf8',errors='replace').lower()
            try:
                parsed=json.loads(detail)
                error=parsed.get('error',parsed)
                code=str(error.get('code','')) if isinstance(error,dict) else ''
            except (ValueError,AttributeError): code=''
            if not re.fullmatch(r'[a-z0-9_.-]{1,80}',code) or code.startswith('sk-'): code=''
            quota = any(x in detail for x in ('quota','resource_exhausted','credits','insufficient balance','neurons'))
            signature = exc.code==403 and 'error code: 1010' in detail
            reason = ('Cloudflare拒绝客户端标识（1010）' if signature else
                '额度不足或限流' if quota or exc.code==429 else
                '密钥无效或无访问权限' if exc.code in {401,403} else '服务暂时失败或请求无效')
            reason={
                'allocationquota.freetieronly':'免费额度已耗尽且开启了用完即停；付费使用需在百炼关闭该开关并确保账号可计费',
                'arrearage':'百炼账号余额或计费状态异常',
                'accessdenied.unpurchased':'百炼服务尚未开通或没有模型权限',
                'invalidapikey':'API Key无效，请核对地域及接口地址',
            }.get(code,reason)
            if code: reason+='（'+code+'）'
            raise ProviderFailure(exc.code, exc.code in {408,429,500,502,503,504} or signature or (exc.code in {402,403} and quota),
                self.service['label']+' · '+self.name, reason) from None
        except URLError as exc:
            reason = exc.reason
            timed_out = isinstance(reason, TimeoutError) or 'timed out' in str(reason).lower()
            if timed_out or isinstance(exc, TimeoutError):
                raise ProviderFailure(408, True, self.service['label']+' · '+self.name, f'请求超时（{self.timeout:g}s）') from None
            raise
        except TimeoutError:
            raise ProviderFailure(408, True, self.service['label']+' · '+self.name, f'请求超时（{self.timeout:g}s）') from None

    def _text(self, result):
        if self.service['protocol']=='gemini':
            return ''.join(p.get('text','') for p in (result.get('candidates') or [{}])[0].get('content',{}).get('parts',[]) if not p.get('thought'))
        return (result.get('choices') or [{}])[0].get('message',{}).get('content','')

    def chat(self, messages, max_tokens=None):
        with self._open(self._payload(messages,max_tokens=max_tokens)) as response:
            result = json.load(response)
        text = self._text(result)
        if not text: raise RuntimeError('模型未返回正文，可能被拦截或输出被截断')
        usage = result.get('usageMetadata') or result.get('usage') or {}
        return ModelResponse(text=text,model=self.name,metadata={'usage':usage,'service':self.service['label']})

    def _messages(self, request):
        content = [{'type':'text','text':build_user(request)}]
        for p in request.input_parts:
            if p.get('kind')=='image' and p.get('image_url'):
                if p.get('label'): content.append({'type':'text','text':p['label']+'（关联历史事件的照片，以照片时间为准）'})
                content.append({'type':'image_url','image_url':{'url':p['image_url']}})
        return [{'role':'system','content':build_system(request)},{'role':'user','content':content if len(content)>1 else content[0]['text']}]

    def generate(self, request):
        return self.chat(self._messages(request))

    def generate_stream(self, request):
        with self._open(self._payload(self._messages(request),stream=True)) as response:
            for line in response:
                line = line.decode('utf8').strip()
                if not line.startswith('data:'): continue
                data = line[5:].strip()
                if data=='[DONE]': break
                event = json.loads(data)
                if self.service['protocol']=='gemini': text = self._text(event)
                else: text = (event.get('choices') or [{}])[0].get('delta',{}).get('content','')
                if text: yield text


def _model_name(model):
    return str(getattr(model, 'name', None) or type(model).__name__)


def is_lite_model(model):
    """Gemini flash-lite (and test stubs named *lite*) used as JEV slow fallback."""
    name = _model_name(model).lower()
    return 'flash-lite' in name or name.endswith('-lite') or name == 'lite'


class _TimeoutBound:
    """Delegate generate while advertising a capped HTTP timeout (stubs + ProviderModel)."""

    def __init__(self, model, timeout):
        self._model = model
        self.timeout = float(timeout)

    def __getattr__(self, name):
        return getattr(self._model, name)

    def generate(self, request):
        model = self._model
        if isinstance(model, ProviderModel):
            return ProviderModel(model.service, model.name, model.effort, model.max_tokens,
                                timeout=self.timeout).generate(request)
        previous = getattr(model, 'timeout', None)
        if previous is not None:
            model.timeout = self.timeout
            try:
                return model.generate(request)
            finally:
                model.timeout = previous
        return model.generate(request)


def _cap_model_timeout(model, remaining_s):
    """Bound one model HTTP wait by remaining (or reserved) gate budget."""
    if remaining_s <= 0:
        raise ProviderFailure(408, True, 'JEV gate', 'wall-clock budget exhausted')
    timeout = getattr(model, 'timeout', None)
    if timeout is None:
        return model
    capped = min(float(timeout), float(remaining_s))
    if capped <= 0:
        raise ProviderFailure(408, True, 'JEV gate', 'wall-clock budget exhausted')
    if isinstance(model, ProviderModel):
        return ProviderModel(model.service, model.name, model.effort, model.max_tokens, timeout=capped)
    return _TimeoutBound(model, capped)


class RoutedModel:
    """Swap an immutable chain. A started request keeps its original chain."""
    def __init__(self, initial, on_paid=None, on_status=None):
        self.chain = (initial,)
        self.lock = RLock()
        self.last = {}
        self.on_paid = on_paid
        self.on_status = on_status

    @property
    def name(self): return getattr(self.chain[0],'name',type(self.chain[0]).__name__)

    def replace(self, chain):
        with self.lock: self.chain = tuple(chain)

    def _snapshot(self):
        with self.lock: return self.chain

    @staticmethod
    def _fallback_reason(exc):
        if isinstance(exc, ProviderFailure):
            status = getattr(exc, 'status', None)
            if status == 408: return 'timeout'
            if status == 429: return '429'
            if status is not None: return str(status)
            msg = str(exc)
            if '超时' in msg or '408' in msg: return 'timeout'
            if '429' in msg or '额度' in msg: return '429'
        if isinstance(exc, (TimeoutError, URLError)):
            return 'timeout'
        if getattr(exc, 'code', None) == 408:
            return 'timeout'
        return type(exc).__name__

    @classmethod
    def _is_timeout(cls, exc):
        return cls._fallback_reason(exc) == 'timeout'

    @contextmanager
    def _observe(self, model):
        call_id, started = uuid4().hex, monotonic()
        def emit(state):
            if self.on_status:
                try:
                    self.on_status({'call_id': call_id, 'state': state,
                        'model': getattr(model, 'name', type(model).__name__),
                        'elapsed_ms': round((monotonic()-started)*1000)})
                except Exception:
                    pass  # UI observation must not alter model execution.
        emit('calling')
        try:
            yield
        except GeneratorExit:
            emit('cancelled')
            raise
        except BaseException:
            emit('failed')
            raise
        else:
            emit('completed')

    def _notify_paid(self, model):
        name = getattr(model, 'name', type(model).__name__)
        if (getattr(model, 'service', {}).get('paid') or name.startswith('deepseek')) and self.on_paid:
            self.on_paid(name)

    def _note(self, model, index, *, degraded=False, fallback_reason='', failed=()):
        name=getattr(model,'name',type(model).__name__)
        with self.lock:
            self.last = {
                'model': name,
                'service': getattr(model,'service',{}).get('label','原配置'),
                'fallback': bool(index) or degraded,
                'degraded': bool(degraded or index),
                'fallback_reason': fallback_reason or '',
                'failed': list(failed),
            }

    def _retryable(self, exc):
        return isinstance(exc,ProviderFailure) and exc.retryable or isinstance(exc,(TimeoutError,ConnectionError,URLError))

    def chat(self, messages, max_tokens=None):
        chain = self._snapshot()
        failed = []
        for index, model in enumerate(chain):
            try:
                with self._observe(model):
                    self._notify_paid(model)
                    result = model.chat(messages,max_tokens=max_tokens)
                reason = failed[0]['reason'] if failed else ''
                self._note(model, index, degraded=bool(failed), fallback_reason=reason, failed=failed)
                return result
            except Exception as exc:
                failed.append({'model': getattr(model,'name',type(model).__name__), 'reason': self._fallback_reason(exc)})
                if index+1==len(chain) or not self._retryable(exc):
                    self._note(model, index, degraded=bool(failed), fallback_reason=self._fallback_reason(exc), failed=failed)
                    raise

    def generate(self, request):
        chain = self._snapshot()
        failed = []
        for index, model in enumerate(chain):
            try:
                with self._observe(model):
                    self._notify_paid(model)
                    result = model.generate(request)
                reason = failed[0]['reason'] if failed else ''
                self._note(model, index, degraded=bool(failed), fallback_reason=reason, failed=failed)
                return result
            except Exception as exc:
                failed.append({'model': getattr(model,'name',type(model).__name__), 'reason': self._fallback_reason(exc)})
                if index+1==len(chain) or not self._retryable(exc):
                    self._note(model, index, degraded=bool(failed), fallback_reason=self._fallback_reason(exc), failed=failed)
                    raise

    def generate_stream(self, request):
        chain = self._snapshot()
        failed = []
        for index, model in enumerate(chain):
            emitted = False
            try:
                with self._observe(model):
                    self._notify_paid(model)
                    if hasattr(model,'generate_stream'): chunks = model.generate_stream(request)
                    else: chunks = (model.generate(request).text,)
                    for chunk in chunks:
                        if chunk:
                            emitted = True
                            yield chunk
                    if not emitted: raise RuntimeError('模型未返回正文')
                reason = failed[0]['reason'] if failed else ''
                self._note(model, index, degraded=bool(failed), fallback_reason=reason, failed=failed)
                return
            except Exception as exc:
                # Never splice two providers into the same partly delivered answer.
                failed.append({'model': getattr(model,'name',type(model).__name__), 'reason': self._fallback_reason(exc)})
                if emitted or index+1==len(chain) or not self._retryable(exc):
                    self._note(model, index, degraded=bool(failed), fallback_reason=self._fallback_reason(exc), failed=failed)
                    raise
