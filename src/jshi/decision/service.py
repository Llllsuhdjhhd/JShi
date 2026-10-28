"""Per-process settings with per-call snapshots; global chat settings never override it."""
import asyncio
import json
import os
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from dataclasses import asdict, replace
from threading import Lock
from uuid import uuid4

from .base import DecisionRequest, DecisionResult
from .system_one import SystemOnePort
from .policies import COLLISION, CONTINUATION, VERSION


def defaults():
    return {'provider': os.getenv('JSHI_DECISION_PROVIDER', 'bailian'),
        'mode': os.getenv('JSHI_DECISION_MODE', 'shadow'),
        'collision': True, 'continuation': True, 'preparation': False,
        'timeout_ms': 800, 'threshold': .85, 'prepare_min_chars': 40,
        'providers': {
            'bailian': {'model': os.getenv('JSHI_DECISION_BAILIAN_MODEL', 'decision-model-preview'),
                'endpoint': os.getenv('JSHI_DECISION_BAILIAN_ENDPOINT', ''),
                'api_key_env': next((k for k in ('DASHSCOPE_API_KEY', 'JSHI_API_QWEN_API_KEY') if os.getenv(k)), 'DASHSCOPE_API_KEY')},
            'typesafe': {'model': os.getenv('JSHI_DECISION_TYPESAFE_MODEL', 'jev-latest'),
                'endpoint': os.getenv('JSHI_DECISION_TYPESAFE_ENDPOINT', 'https://api.typesafe.ai/v1/systemone'),
                'api_key_env': 'TYPESAFE_API_KEY'}}}


def validate(config):
    if config['provider'] not in {'bailian', 'typesafe'} or config['mode'] not in {'off', 'shadow', 'active'}:
        raise ValueError('决策供应商或模式无效')
    if not 50 <= config['timeout_ms'] <= 5000 or not .5 <= config['threshold'] <= 1:
        raise ValueError('决策等待预算或阈值无效')
    if not 10 <= config['prepare_min_chars'] <= 2000:
        raise ValueError('准备字数无效')
    for flag in ('collision', 'continuation', 'preparation'):
        if type(config[flag]) is not bool:
            raise ValueError('功能开关无效')
    for name in ('bailian', 'typesafe'):
        provider = config['providers'][name]
        endpoint = urlsplit(provider['endpoint'])
        if provider['endpoint'] and (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment or not endpoint.path.endswith('/v1/systemone')):
            raise ValueError('请填写不含密钥的HTTPS System One完整地址')
        # A key cannot silently be forwarded to the other provider.
        suffix = 'aliyuncs.com' if name == 'bailian' else 'typesafe.ai'
        if provider['endpoint'] and endpoint.hostname != suffix and not endpoint.hostname.endswith('.' + suffix):
            raise ValueError('地址与所选决策供应商不符')
        import re
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,100}', provider['api_key_env']):
            raise ValueError('请填写密钥的环境变量名')
        if not provider['model'] or len(provider['model']) > 160:
            raise ValueError('模型名称无效')
    return config


class DecisionService:
    def __init__(self, root, *, port_factory=SystemOnePort):
        self.path = Path(root) / 'decision_settings.local.json'
        self.config = defaults()
        if self.path.exists():
            try:
                self.config = validate(json.loads(self.path.read_text(encoding='utf8')))
            except (ValueError, KeyError, TypeError):
                self.config = defaults()
        self.factory, self.ports = port_factory, {}
        self.root = Path(root)
        self.last = {}
        self.cooldown = {}
        self.slots = asyncio.Semaphore(2)
        self.audit_lock = Lock()

    def record(self, value):
        """Local diagnostic request/answer, never headers or credentials; bounded rotation."""
        path = self.root/'decision_calls.jsonl'
        try:
            with self.audit_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size > 20*1024*1024:
                    path.replace(path.with_suffix('.jsonl.1'))
                with path.open('a', encoding='utf8') as output:
                    output.write(json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n')
        except (OSError, TypeError, ValueError):
            pass  # Diagnostics never change routing or expose raw data in errors.

    def public(self):
        config = json.loads(json.dumps(self.config))
        for name, p in config['providers'].items():
            key, source = self.credentials(name, p)
            p['has_key'] = bool(key)
            p['has_endpoint'] = bool(p['endpoint'])
            p['credential_source'] = source
        return {**config, 'last': dict(self.last)}

    def credentials(self, provider, options):
        key = os.getenv(options['api_key_env'], '')
        if key:
            return key, 'environment'
        # Existing Bailian settings are already local credentials. Do not duplicate
        # them into a new file or borrow an unrelated model/vision/TTS key.
        if provider == 'bailian' and options['api_key_env'] in {'DASHSCOPE_API_KEY', 'JSHI_API_QWEN_API_KEY'}:
            try:
                saved = json.loads((self.root/'model_settings.local.json').read_text(encoding='utf8'))
                qwen = saved.get('services', {}).get('qwen', {})
                host = urlsplit(qwen.get('endpoint', '')).hostname or ''
                if host == 'dashscope.aliyuncs.com' or host.endswith('.maas.aliyuncs.com'):
                    return qwen.get('api_key', ''), 'existing_qwen_settings'
            except (OSError, ValueError, TypeError):
                pass
        return '', 'not_configured'

    def save(self, raw):
        config = json.loads(json.dumps(self.config))
        for key in ('provider', 'mode', 'collision', 'continuation', 'preparation', 'timeout_ms', 'threshold', 'prepare_min_chars'):
            if key in raw:
                config[key] = raw[key]
        for name, provider in raw.get('providers', {}).items():
            if name not in config['providers'] or set(provider) - {'endpoint', 'model', 'api_key_env'}:
                raise ValueError('决策服务配置字段无效')
            config['providers'][name].update(provider)
        validate(config)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf8')
        temp.replace(self.path)
        self.config = config
        return self.public()

    async def decide(self, purpose, state, revision=0, *, force=False):
        started = monotonic()
        if purpose not in {'collision', 'continuation'}:
            return DecisionResult('invalid', reason='unknown_purpose')
        config = json.loads(json.dumps(self.config))
        if not force and (config['mode'] == 'off' or not config.get(purpose, False)):
            return DecisionResult('unavailable', reason='disabled')
        provider = config['provider']
        options = config['providers'][provider]
        key, _source = self.credentials(provider, options)
        cache = (provider, options['model'], options['endpoint'], key)
        if self.cooldown.get(cache, 0) > monotonic() and not force:
            return DecisionResult('unavailable', reason='cooldown')
        if cache not in self.ports:
            self.ports[cache] = self.factory(provider, options['model'], options['endpoint'], key)
        questions = COLLISION if purpose == 'collision' else CONTINUATION
        request = DecisionRequest(purpose, state, questions, revision)
        deadline = monotonic() + config['timeout_ms']/1000
        call_id = uuid4().hex
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.to_thread(self.record, {'kind':'request','id':call_id,'purpose':purpose,
                    'revision':revision,'policy':VERSION,'provider':provider,
                    'body':{'model':options['model'],'state':state,'questions':questions}})
                async with self.slots:
                    result = await self.ports[cache].decide(request, deadline=deadline)
        except asyncio.TimeoutError:
            result = DecisionResult('timeout', reason='queue_deadline')
        transport_ms = result.elapsed_ms
        result = replace(result, elapsed_ms=round((monotonic()-started)*1000, 2))
        self.last = {'purpose': purpose, 'status': result.status, 'model': result.model,
            'elapsed_ms': result.elapsed_ms, 'server_ms': result.server_ms,
            'transport_ms':transport_ms,
            'reason': result.reason, 'answers': result.answers, 'revision': revision, 'policy': VERSION}
        if result.status in {'timeout', 'invalid', 'unavailable'}:
            self.cooldown[cache] = monotonic() + 10
        await asyncio.to_thread(self.record, {'kind':'result','id':call_id,**asdict(result)})
        return result

    async def test(self):
        return await self.decide('collision', {'old': [{'text': '请查明天去杭州的车次'}],
            'new': [{'text': '改成后天出发'}]}, force=True)

    async def close(self):
        for port in self.ports.values():
            await port.close()
        self.ports.clear()


def decision_service(process):
    service = getattr(process, 'decision_service', None)
    if service is None:
        service = DecisionService(process.repository.path.parent)
        process.decision_service = service
    return service
