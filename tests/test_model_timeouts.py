"""Offline regression for responses that send headers before a slow body."""
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic, sleep
from types import SimpleNamespace
from urllib.request import ProxyHandler, build_opener

import pytest

from jshi.app.model_settings import COGNITION_TIMEOUT_S, ModelSettings
from jshi.models import call_log, http_pool
from jshi.models.routing import ProviderModel, ProviderTimeout, RoutedModel


def service(endpoint='http://127.0.0.1:1'):
    return {'label': '本地测试', 'endpoint': endpoint, 'protocol': 'chat', 'api_key': 'test-only'}


def chain(purpose, endpoint='http://127.0.0.1:1'):
    return ModelSettings._chain(None, {'choices': [{'service': 'local', 'model': 'test-model'}]},
                                {'local': service(endpoint)}, purpose=purpose)


@pytest.fixture
def records(monkeypatch):
    rows = []
    monkeypatch.setattr(call_log, 'record', rows.append)
    return rows


@pytest.fixture
def delayed_server(monkeypatch):
    state = SimpleNamespace(delay=0.0, requests=0)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            state.requests += 1
            body = json.dumps({'choices': [{'message': {'content': '本地响应已完成'}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.flush()
            sleep(state.delay)
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # The short-timeout test deliberately closes before the body.

        def log_message(self, *args):
            pass

    # The probe stays on loopback even when the machine has a configured proxy.
    monkeypatch.setattr(http_pool, '_plain_urlopen', build_opener(ProxyHandler({})).open)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.endpoint = f'http://127.0.0.1:{server.server_port}'
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cognition_can_read_a_body_delayed_beyond_old_seven_second_limit(delayed_server, records):
    delayed_server.delay = ProviderModel.DEFAULT_TIMEOUT_S + 0.3
    model = chain('cognition', delayed_server.endpoint)[0]
    started = monotonic()
    reply = model.chat([{'role': 'user', 'content': '本地延迟测试'}], purpose='subject_activity')
    elapsed = monotonic() - started
    assert reply.text == '本地响应已完成'
    assert ProviderModel.DEFAULT_TIMEOUT_S < elapsed < COGNITION_TIMEOUT_S
    assert delayed_server.requests == 1  # No repeated inference or execution.
    assert records[-1]['timeout_s'] == COGNITION_TIMEOUT_S
    assert records[-1]['status'] == 'ok'


def test_body_timeout_is_clear_and_single_attempt(delayed_server, records):
    delayed_server.delay = 0.3
    model = ProviderModel(service(delayed_server.endpoint), 'test-model', timeout=0.05)
    with pytest.raises(ProviderTimeout, match='读取模型回答正文超时') as exc:
        RoutedModel(model).chat([{'role': 'user', 'content': '本地超时测试'}])
    assert exc.value.retryable and exc.value.status == 408
    assert 'HTTP 408' not in str(exc.value)  # The server actually returned HTTP 200.
    assert delayed_server.requests == 1
    assert records[-1]['error_phase'] == 'response_body'
    assert records[-1]['error_type'] == 'TimeoutError'


@pytest.mark.parametrize('purpose,limit', [
    ('cognition', COGNITION_TIMEOUT_S), ('voice_jev', 7.0),
    ('vision', 7.0), ('tool_wrap', 7.0), ('tool_plan', 45.0),
])
def test_route_limits_do_not_slow_the_gate(purpose, limit):
    assert chain(purpose)[0].timeout == limit


def test_header_timeout_does_not_claim_the_body_failed(monkeypatch, records):
    def fail(*args, **kwargs):
        raise TimeoutError('timed out')
    monkeypatch.setattr('jshi.models.routing.urlopen', fail)
    with pytest.raises(ProviderTimeout, match='连接或等待模型响应头超时'):
        chain('cognition')[0].chat([])
    assert records[-1]['error_phase'] == 'connection_or_headers'


def test_stream_body_timeout_after_partial_reply_never_splices_a_fallback(monkeypatch, records):
    class Response(io.BytesIO):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n'
            raise TimeoutError('The read operation timed out')

    monkeypatch.setattr('jshi.models.routing.urlopen', lambda *a, **k: Response())
    model = chain('cognition')[0]
    monkeypatch.setattr(model, '_messages', lambda request: [])
    class Unused:
        name = 'unused'
        def generate_stream(self, request):
            raise AssertionError('must not combine answers after partial delivery')

    routed = RoutedModel(model)
    routed.replace([model, Unused()])
    request = SimpleNamespace(purpose='subject_activity', json_output=False, persona_schema=None)
    stream = routed.generate_stream(request)
    assert next(stream) == 'partial'
    with pytest.raises(ProviderTimeout, match='读取模型回答正文超时'):
        next(stream)
    assert records[-1]['error_phase'] == 'response_body'
    assert routed.last['fallback_reason'] == 'timeout'
