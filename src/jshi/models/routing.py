"""Provider adapters and per-request snapshots for live model routing."""
from contextlib import contextmanager
from time import monotonic
from uuid import uuid4
import json
import re
from threading import RLock
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request

from jshi.models.http_pool import urlopen

from .base import ModelResponse, deepseek_json_output
from .prompt import build_system, build_user


class ProviderFailure(RuntimeError):
    def __init__(self, status, retryable, context='', reason=''):
        super().__init__(f'{context+"：" if context else ""}模型服务返回HTTP {status}{"；"+reason if reason else ""}')
        self.status = status
        self.retryable = retryable
        self.reason = reason


class ProviderTimeout(ProviderFailure):
    """A local socket timeout, not an HTTP 408 returned by the provider."""
    def __init__(self, timeout, context, phase):
        self.status, self.retryable = 408, True
        self.reason = (f'读取模型回答正文超时（连续{timeout:g}秒未收到数据）' if phase == 'response_body'
                       else f'连接或等待模型响应头超时（网络等待上限{timeout:g}秒）')
        RuntimeError.__init__(self, f'{context}：{self.reason}')


class ProviderModel:
    # Short-route socket inactivity limit. Main cognition and background routes
    # pass their own limits; this is not an end-to-end latency guarantee.
    DEFAULT_TIMEOUT_S = 7.0

    def __init__(self, service, model, effort='low', max_tokens=None, timeout=None, route=''):
        self.service, self.name, self.effort = dict(service), model, effort
        self.max_tokens = max_tokens
        self.timeout = self.DEFAULT_TIMEOUT_S if timeout is None else float(timeout)
        self.route = route

    def _with_timeout(self, timeout):
        return ProviderModel(self.service, self.name, self.effort, self.max_tokens, timeout=timeout, route=self.route)

    def _lowest_effort(self):
        if self.name.startswith('gemini-3'):
            return 'low' if self.name == 'gemini-3.8-flash' else 'minimal'
        return 'off'

    def _for_request(self, request):
        """L3 requests keep prompt and schema; only thinking drops to the model's lowest level."""
        if not getattr(request, 'thinking_off', False) or self.effort == self._lowest_effort():
            return self
        return ProviderModel(self.service, self.name, self._lowest_effort(), self.max_tokens,
                             timeout=self.timeout, route=self.route)

    def _log(self, started, response, *, purpose, stream, usage=None, error=None, error_phase=None):
        from .call_log import record, usage_fields
        transport = getattr(response, 'transport', None) or {}
        record({'route': self.route, 'purpose': purpose, 'service': self.service.get('label', ''),
                'model': self.name, 'effort': self.effort, 'stream': stream, 'status': 'error' if error else 'ok',
                **({'error_type': error} if error else {}),
                **({'error_phase': error_phase} if error_phase else {}),
                'timeout_s': self.timeout,
                'reused': transport.get('reused'), 'connect_ms': transport.get('connect_ms'),
                'headers_ms': transport.get('headers_ms'), 'idle_s': transport.get('idle_s'),
                'stale_retry': transport.get('stale_retry'),
                'total_ms': round((monotonic() - started) * 1000, 1), **usage_fields(usage)})

    def _timeout(self, phase):
        return ProviderTimeout(self.timeout, self.service['label']+' · '+self.name, phase)

    def _payload(self, messages, stream=False, max_tokens=None, response_format=None):
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
            if response_format == 'json_object':
                config['responseMimeType'] = 'application/json'
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
            if stream and 'deepseek.com' in (urlsplit(service['endpoint']).hostname or ''):
                body['stream_options'] = {'include_usage': True}
            if response_format:
                body['response_format'] = {'type': response_format}
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
                raise self._timeout('connection_or_headers') from None
            raise
        except TimeoutError:
            raise self._timeout('connection_or_headers') from None

    def _text(self, result):
        if self.service['protocol']=='gemini':
            return ''.join(p.get('text','') for p in (result.get('candidates') or [{}])[0].get('content',{}).get('parts',[]) if not p.get('thought'))
        return (result.get('choices') or [{}])[0].get('message',{}).get('content','')

    def chat(self, messages, max_tokens=None, *, response_format=None, purpose=''):
        started, response = monotonic(), None
        try:
            with self._open(self._payload(messages,max_tokens=max_tokens, response_format=response_format)) as response:
                result = json.load(response)
        except BaseException as exc:
            phase = 'response_body' if response is not None else 'connection_or_headers'
            self._log(started, response, purpose=purpose, stream=False, error=type(exc).__name__, error_phase=phase)
            if isinstance(exc, TimeoutError):
                raise self._timeout(phase) from exc
            raise
        self._log(started, response, purpose=purpose, stream=False,
                  usage=result.get('usageMetadata') or result.get('usage'))
        text = self._text(result)
        if not text: raise RuntimeError('模型未返回正文，可能被拦截或输出被截断')
        if not response_format and not text.lstrip().startswith(('{', '[')):
            from jshi.simplified import simplify_text
            text = simplify_text(text)
        usage = result.get('usageMetadata') or result.get('usage') or {}
        finish = (result.get('choices') or [{}])[0].get('finish_reason')
        return ModelResponse(text=text,model=self.name,metadata={'usage':usage,'service':self.service['label'],
            'finish_reason': finish, 'response_format': response_format})

    def _messages(self, request):
        content = [{'type':'text','text':build_user(request)}]
        for p in request.input_parts:
            if p.get('kind')=='image' and p.get('image_url'):
                if p.get('label'): content.append({'type':'text','text':p['label']+'（关联历史事件的照片，以照片时间为准）'})
                content.append({'type':'image_url','image_url':{'url':p['image_url']}})
        return [{'role':'system','content':build_system(request)},{'role':'user','content':content if len(content)>1 else content[0]['text']}]

    def generate(self, request):
        if getattr(request, 'light_finalize', False):
            from dataclasses import replace
            effort = 'low' if self.service['protocol'] == 'gemini' else 'off'
            return ProviderModel(self.service, self.name, effort, self.max_tokens, self.timeout, self.route).generate(
                replace(request, light_finalize=False))
        model = self._for_request(request)
        if model is not self:
            return model.generate(request)
        if deepseek_json_output(request, self.name) and self.service['protocol'] != 'gemini':
            return self.chat(self._messages(request),
                             max_tokens=self.max_tokens or (8192 if request.purpose.startswith('voice_jev') else None),
                             response_format='json_object', purpose=request.purpose)
        return self.chat(self._messages(request), purpose=request.purpose)

    def generate_stream(self, request):
        model = self._for_request(request)
        if model is not self:
            yield from model.generate_stream(request)
            return
        options = ({'response_format': 'json_object',
                    'max_tokens': self.max_tokens or (8192 if request.purpose.startswith('voice_jev') else None)}
                   if deepseek_json_output(request, self.name) and self.service['protocol'] != 'gemini' else {})
        started, response, usage, error = monotonic(), None, None, None
        try:
            with self._open(self._payload(self._messages(request),stream=True, **options)) as response:
                for line in response:
                    line = line.decode('utf8').strip()
                    if not line.startswith('data:'): continue
                    data = line[5:].strip()
                    if data=='[DONE]': break
                    event = json.loads(data)
                    usage = event.get('usageMetadata') or event.get('usage') or usage
                    if self.service['protocol']=='gemini': text = self._text(event)
                    else: text = (event.get('choices') or [{}])[0].get('delta',{}).get('content','')
                    if text: yield text
        except BaseException as exc:
            error = 'cancelled' if isinstance(exc, GeneratorExit) else type(exc).__name__
            if isinstance(exc, TimeoutError):
                raise self._timeout('response_body' if response is not None else 'connection_or_headers') from exc
            raise
        finally:
            self._log(started, response, purpose=request.purpose, stream=True, usage=usage, error=error,
                      error_phase=('response_body' if response is not None else 'connection_or_headers')
                      if error and error != 'cancelled' else None)


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
            return model._with_timeout(self.timeout).generate(request)
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
        return model._with_timeout(capped)
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

    def chat(self, messages, max_tokens=None, *, response_format=None):
        chain = self._snapshot()
        failed = []
        for index, model in enumerate(chain):
            try:
                with self._observe(model):
                    self._notify_paid(model)
                    options = {'response_format': response_format} if response_format else {}
                    result = model.chat(messages,max_tokens=max_tokens, **options)
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
