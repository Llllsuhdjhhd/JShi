import io
import json

import pytest

from jshi.models import call_log, http_pool
from jshi.models.routing import ProviderModel
from jshi.voice.diagnostics import diagnostic_log


class FakeResponse(io.BytesIO):
    status, reason, headers, will_close = 200, 'OK', {}, False

    def isclosed(self):
        return self.tell() == len(self.getvalue())


class FakeConnection:
    opened = []

    def __init__(self, host, port, timeout=None, context=None):
        self.sock, self.timeout, self.body = None, timeout, b'{}'
        FakeConnection.opened.append(self)

    def connect(self):
        self.sock = io.BytesIO()
        self.sock.settimeout = lambda value: None

    def request(self, *args, **kwargs):
        if self.sock is None:
            self.connect()

    def getresponse(self):
        return FakeResponse(self.body)

    def close(self):
        self.sock = None


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr(http_pool.http.client, 'HTTPSConnection', FakeConnection)
    monkeypatch.setattr(http_pool, 'getproxies', lambda: {})
    monkeypatch.setattr(http_pool, '_idle', {})
    FakeConnection.opened = []


def call(url='https://api.deepseek.com/chat/completions'):
    response = http_pool.urlopen(http_pool.Request(url, data=b'{}', method='POST'), timeout=5)
    response.read()
    response.close()
    return response.transport


def test_transport_reports_new_then_reused_connection(pool):
    first, second = call(), call()
    assert first['reused'] is False and first['connect_ms'] is not None and first['idle_s'] is None
    assert second['reused'] is True and second['connect_ms'] is None and second['idle_s'] is not None
    assert len(FakeConnection.opened) == 1


def test_prewarm_opens_once_and_next_call_reuses_it(pool):
    assert http_pool.prewarm('https://api.deepseek.com/chat/completions')['status'] == 'connected'
    assert http_pool.prewarm('https://api.deepseek.com/chat/completions')['status'] == 'fresh_idle'
    assert call()['reused'] is True and len(FakeConnection.opened) == 1


def test_prewarm_keeps_one_connection_per_concurrent_route(pool):
    url = 'https://api.deepseek.com/chat/completions'
    assert http_pool.prewarm(url, count=2)['status'] == 'connected'
    assert len(FakeConnection.opened) == 2
    assert http_pool.prewarm(url, count=2)['status'] == 'fresh_idle'


def test_reused_connection_closed_by_gateway_retries_on_a_new_one(pool):
    import ssl
    call()
    for stale in FakeConnection.opened:
        stale.request = lambda *a, **k: (_ for _ in ()).throw(ssl.SSLEOFError('EOF occurred'))
    http_pool._park(('api.deepseek.com', 443, ''), FakeConnection('api.deepseek.com', 443))
    FakeConnection.opened[-1].connect()
    FakeConnection.opened[-1].request = FakeConnection.opened[0].request
    transport = call()
    assert transport['stale_retry'] is True and transport['reused'] is False
    assert len(FakeConnection.opened) == 3


class Transported(io.BytesIO):
    transport = {'reused': False, 'connect_ms': 1500.0, 'headers_ms': 400.0, 'idle_s': None, 'stale_retry': False}


def read_log(path):
    diagnostic_log(path).flush()
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_chat_and_stream_record_connection_and_cache_tokens(tmp_path, monkeypatch):
    path = tmp_path / 'model_calls.jsonl'
    call_log.configure(path)
    sent = []
    usage = {'prompt_tokens': 3000, 'prompt_cache_hit_tokens': 2800, 'prompt_cache_miss_tokens': 200,
             'completion_tokens': 40}
    def fake_open(request, timeout):
        body = json.loads(request.data)
        sent.append(body)
        if body['stream']:
            lines = [b'data: ' + json.dumps({'choices': [{'delta': {'content': 'OK'}}]}).encode(),
                     b'data: ' + json.dumps({'choices': [], 'usage': usage}).encode(), b'data: [DONE]']
            return Transported(b'\n'.join(lines) + b'\n')
        return Transported(json.dumps({'choices': [{'message': {'content': 'OK'}}], 'usage': usage}).encode())
    monkeypatch.setattr('jshi.models.routing.urlopen', fake_open)
    model = ProviderModel({'label': 'DeepSeek', 'protocol': 'chat', 'endpoint': 'https://api.deepseek.com',
                           'api_key': 'k'}, 'deepseek-v4-flash', route='voice_jev')
    try:
        model.chat([{'role': 'user', 'content': 'hi'}], purpose='voice_jev_entry')
        from types import SimpleNamespace
        model._messages = lambda request: [{'role': 'user', 'content': 'hi'}]
        request = SimpleNamespace(purpose='subject_activity', json_output=False, persona_schema=None)
        assert list(model.generate_stream(request)) == ['OK']
        rows = read_log(path)
    finally:
        call_log.configure(None)
    assert sent[1]['stream_options'] == {'include_usage': True} and 'stream_options' not in sent[0]
    for row, stream in zip(rows, (False, True)):
        assert row['route'] == 'voice_jev' and row['stream'] is stream and row['status'] == 'ok'
        assert row['reused'] is False and row['connect_ms'] == 1500.0 and row['headers_ms'] == 400.0
        assert row['prompt_tokens'] == 3000 and row['cache_hit_tokens'] == 2800 and row['cache_miss_tokens'] == 200
    assert rows[0]['purpose'] == 'voice_jev_entry' and rows[1]['purpose'] == 'subject_activity'
    assert 'messages' not in json.dumps(rows) and 'k' not in {v for r in rows for v in r.values() if isinstance(v, str)}


def test_speech_start_prewarm_is_limited_per_interval(monkeypatch):
    from types import SimpleNamespace
    from threading import RLock
    from jshi.app.model_settings import ModelSettings
    started = []
    monkeypatch.setattr('jshi.app.model_settings.threading.Thread',
                        lambda target, **kw: SimpleNamespace(start=lambda: started.append(target)))
    model = SimpleNamespace(service={'endpoint': 'https://api.deepseek.com'})
    settings = SimpleNamespace(lock=RLock(), prewarmed_at=float('-inf'),
                               routes={'voice_jev': SimpleNamespace(chain=(model,)),
                                       'cognition': SimpleNamespace(chain=(model,))})
    assert ModelSettings.prewarm(settings) is True
    assert ModelSettings.prewarm(settings) is False
    assert len(started) == 1
    warmed = []
    monkeypatch.setattr('jshi.models.http_pool.prewarm', lambda url, count=1: warmed.append(count) or {'status': 'connected'})
    monkeypatch.setattr('jshi.models.call_log.record', lambda row: None)
    started[0]()
    assert warmed == [2]


def test_stream_consumer_reads_final_usage_after_json_closes():
    from jshi.models.base import finish_stream, iter_top_level_json_values
    seen = []
    def chunks():
        yield '{"a": 1}'
        seen.append('usage')
    stream = chunks()
    assert [k for k, _ in iter_top_level_json_values(stream)] == ['a']
    finish_stream(stream)
    assert seen == ['usage']


def test_l3_request_turns_thinking_off_without_changing_the_prompt(tmp_path, monkeypatch):
    from types import SimpleNamespace
    sent = []
    def fake_open(request, timeout):
        sent.append(json.loads(request.data))
        return Transported(json.dumps({'choices': [{'message': {'content': '{}'}}]}).encode())
    monkeypatch.setattr('jshi.models.routing.urlopen', fake_open)
    model = ProviderModel({'label': 'DeepSeek', 'protocol': 'chat', 'endpoint': 'https://api.deepseek.com',
                           'api_key': 'k', 'thinking_supported': True}, 'deepseek-v4-flash', 'low', route='cognition')
    messages = [{'role': 'user', 'content': 'hi'}]
    monkeypatch.setattr(ProviderModel, '_messages', lambda self, request: messages)
    for off in (False, True):
        model.generate(SimpleNamespace(purpose='subject_activity', json_output=True, persona_schema=None,
                                       light_finalize=False, thinking_off=off))
    assert sent[0]['thinking'] == {'type': 'enabled'} and sent[0]['reasoning_effort'] == 'low'
    assert sent[1]['thinking'] == {'type': 'disabled'} and 'reasoning_effort' not in sent[1]
    assert sent[0]['messages'] == sent[1]['messages'] and model.effort == 'low'
    gemini = ProviderModel({'label': 'G', 'protocol': 'gemini', 'endpoint': 'https://g', 'api_key': 'k'},
                           'gemini-3.8-flash', 'high')
    assert gemini._for_request(SimpleNamespace(thinking_off=True)).effort == 'low'
