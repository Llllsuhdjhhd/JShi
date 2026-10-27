"""JEV gate wall-clock budget: lite timeout → one fallback_models call; never respond."""
from __future__ import annotations

import json
import time
from io import BytesIO

import pytest

from jshi.models import ModelResponse
from jshi.models.routing import ProviderFailure, ProviderModel, RoutedModel
from jshi.voice.jev import JEV_GATE_BUDGET_S, JEV_GATE_FALLBACK_RESERVE_S, VoiceJEV


def _ok_batch(person='P1', why='明确对匠石提问'):
    return json.dumps({
        'items': [{'i': 1, 'person': person, 'certainty': 'confirmed', 'why': why}],
        'level': 2, 'recall_memory': False, 'main_image': 'omit', 'route': 'main', 'reply': '',
    })


def _batch():
    return [{'n': 1, 'text': '匠石，今天天气怎么样？', 'who': 'P1（lux）',
             'voice_evidence': {'pick': 'P1', 'strength': 'clear'},
             'candidates': [{'who': 'P1', 'score': .8}]}]


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += float(dt)


class LiteTimeout:
    name = 'gemini-3.1-flash-lite'
    service = {'label': 'G'}

    def __init__(self, clock, timeout=7.0):
        self.clock = clock
        self.timeout = float(timeout)
        self.calls = 0
        self.last_wait = None

    def generate(self, request):
        self.calls += 1
        # Honour the capped timeout set by _cap_model_timeout / _TimeoutBound.
        self.last_wait = float(self.timeout)
        self.clock.advance(self.last_wait)
        raise ProviderFailure(408, True, 'G · lite', f'请求超时（{self.last_wait:g}s）')


class LiteRetryTrap:
    """Second lite in a chain must never be called after the first lite times out."""
    name = 'gemini-3.1-flash-lite'
    service = {'label': 'G'}

    def __init__(self):
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        raise AssertionError('must not retry lite')


class QwenOk:
    name = 'qwen3.5-flash'
    service = {'label': 'Q', 'thinking_style': 'qwen', 'paid': True}

    def __init__(self, clock, cost=1.0, text=None):
        self.clock = clock
        self.cost = cost
        self.text = text or _ok_batch()
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        self.clock.advance(self.cost)
        return ModelResponse(model=self.name, text=self.text)


class QwenFail:
    name = 'qwen3.5-flash'
    service = {'label': 'Q', 'thinking_style': 'qwen'}

    def __init__(self, clock, cost=1.0):
        self.clock = clock
        self.cost = cost
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        self.clock.advance(self.cost)
        raise ProviderFailure(500, True, 'Q · qwen', '服务暂时失败')


class QwenHang:
    """Ignores HTTP timeout and blocks the worker thread; gate must hard-wait and hold."""
    name = 'qwen3.5-flash'
    service = {'label': 'Q', 'thinking_style': 'qwen'}
    timeout = 7.0

    def __init__(self, sleep_s=10.0):
        self.sleep_s = sleep_s
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        time.sleep(self.sleep_s)
        return ModelResponse(model=self.name, text=_ok_batch(why='should not win'))


def test_budget_s_and_fallback_models_init_defaults(monkeypatch):
    monkeypatch.delenv('JSHI_API_QWEN_API_KEY', raising=False)
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    gate = VoiceJEV(mode='legacy')
    assert gate.budget_s == JEV_GATE_BUDGET_S == 8.0
    assert gate.fallback_reserve_s == JEV_GATE_FALLBACK_RESERVE_S == 3.5
    assert gate.fallback_models == []


def test_lite_timeout_calls_qwen_once_no_lite_retry(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    lite = LiteTimeout(clock, timeout=3.0)
    trap = LiteRetryTrap()
    qwen = QwenOk(clock, cost=1.0)
    primary = RoutedModel(lite)
    primary.replace([lite, trap])
    gate = VoiceJEV(primary, budget_s=8.0, fallback_models=[qwen], mode='legacy')
    started = clock()
    result = gate.decide_batch('stone', _batch(), [], {'state': 'completed'})
    elapsed = clock() - started
    assert result.judged and result.action == 'respond'
    assert lite.calls == 1 and trap.calls == 0 and qwen.calls == 1
    assert elapsed <= 8.0


def test_qwen_success_used_after_lite_timeout(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    lite = LiteTimeout(clock, timeout=2.0)
    qwen = QwenOk(clock, cost=0.5, text=_ok_batch(why='Qwen兜底人物终审'))
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen], mode='legacy')
    result = gate.decide_batch('stone', _batch(), [], {'state': 'completed'})
    assert result.judged and result.action == 'respond' and not result.model_error
    assert result.items[0].speaker_reason == 'Qwen兜底人物终审'
    assert lite.calls == 1 and qwen.calls == 1


def test_lite_runs_to_reserve_cap_then_qwen_judged(monkeypatch):
    """Lite capped at budget_s − fallback_reserve_s (4.5s); Qwen 3.04s → judged, not hold."""
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    lite = LiteTimeout(clock, timeout=7.0)
    qwen = QwenOk(clock, cost=3.04, text=_ok_batch(why='预留后Qwen终审'))
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen], fallback_reserve_s=3.5, mode='legacy')
    started = clock()
    result = gate.decide_batch('stone', _batch(), [], {'state': 'completed'})
    elapsed = clock() - started
    assert result.judged and result.action == 'respond' and not result.model_error
    assert result.items[0].speaker_reason == '预留后Qwen终审'
    assert lite.calls == 1 and qwen.calls == 1
    assert lite.last_wait == 4.5  # min(7, 8 − 3.5)
    assert elapsed == pytest.approx(7.54)
    assert elapsed <= gate.budget_s


def test_qwen_hang_hard_deadline_holds_within_budget(monkeypatch):
    """Hung Qwen (sleep ignores timeout) → hold; wall clock ≤ budget + small epsilon."""
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    # Lite consumes 4.5s on the fake clock → remaining wait for Qwen is 3.5s real.
    lite = LiteTimeout(clock, timeout=7.0)
    qwen = QwenHang(sleep_s=10.0)
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen], fallback_reserve_s=3.5, mode='legacy')
    started = time.monotonic()
    result = gate.decide_batch('stone', _batch(), [], {'state': 'idle'})
    elapsed = time.monotonic() - started
    assert result.action == 'ignore'
    assert not result.judged and result.model_error
    assert result.items[0].to_jiangshi == 'no'
    assert lite.calls == 1 and qwen.calls == 1
    assert lite.last_wait == 4.5
    assert elapsed <= gate.budget_s + 0.75


@pytest.mark.parametrize('state,expected', [
    ('completed', 'ignore'),
    ('idle', 'ignore'),
    ('playing', 'resume'),
    ('paused', 'resume'),
])
def test_budget_exhausted_holds_without_respond(monkeypatch, state, expected):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    # No fallback configured → no reserve; lite may consume the full budget → hold.
    lite = LiteTimeout(clock, timeout=8.0)
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[], mode='legacy')
    started = clock()
    result = gate.decide_batch('stone', _batch(), [], {'state': state})
    elapsed = clock() - started
    assert result.action == expected
    assert not result.judged and result.model_error
    assert result.items[0].to_jiangshi == 'no'
    assert lite.calls == 1
    assert elapsed <= 8.0


def test_qwen_failure_after_lite_timeout_holds(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    lite = LiteTimeout(clock, timeout=2.0)
    qwen = QwenFail(clock, cost=1.0)
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen], mode='legacy')
    result = gate.decide_batch('stone', [{'n': 1, 'text': '你好'}], [], {'state': 'playing'})
    assert result.action == 'resume'
    assert not result.judged and result.model_error
    assert lite.calls == 1 and qwen.calls == 1


def test_total_elapsed_bounded_by_budget_with_fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    lite = LiteTimeout(clock, timeout=4.5)
    qwen = QwenOk(clock, cost=3.0)
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen], mode='legacy')
    started = clock()
    result = gate.decide_batch('stone', _batch(), [], {'state': 'completed'})
    elapsed = clock() - started
    assert result.judged and qwen.calls == 1
    assert elapsed <= gate.budget_s == 8.0


def test_decide_single_also_uses_budget_and_fallback(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    lite = LiteTimeout(clock, timeout=2.0)

    class QwenSingle:
        name = 'qwen3.5-flash'
        service = {'thinking_style': 'qwen'}
        calls = 0

        def generate(self, request):
            self.calls += 1
            clock.advance(0.5)
            return ModelResponse(model=self.name, text=json.dumps({
                'action': 'ignore', 'reason': '旁人闲聊'}))

    qwen = QwenSingle()
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen], mode='legacy')
    decision = gate.decide('stone', '旁人说话', {}, {'state': 'playing'})
    assert decision.action == 'ignore'
    assert lite.calls == 1 and qwen.calls == 1


def test_jev_qwen_fallback_outgoing_payload_has_enable_thinking_false(monkeypatch):
    """Prove DashScope body gets enable_thinking=false on the JEV fallback path (not assumed)."""
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    captured = {}

    class _Resp(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured['body'] = json.loads(request.data.decode('utf8'))
        captured['timeout'] = timeout
        return _Resp(json.dumps({
            'choices': [{'message': {'content': _ok_batch(why='thinking-off终审')}}],
            'usage': {},
        }).encode())

    monkeypatch.setattr('jshi.models.routing.urlopen', fake_urlopen)
    lite = LiteTimeout(clock, timeout=2.0)
    # Intentionally effort='low' so the gate must force off on the fallback path.
    qwen = ProviderModel(
        {'label': 'Q', 'protocol': 'chat', 'thinking_style': 'qwen', 'paid': True,
         'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'api_key': 'test-key'},
        'qwen3.5-flash', 'low', timeout=7.0)
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen], fallback_reserve_s=3.5, mode='legacy')
    result = gate.decide_batch('stone', _batch(), [], {'state': 'completed'})
    assert result.judged and result.action == 'respond'
    assert captured['body']['model'] == 'qwen3.5-flash'
    assert captured['body']['enable_thinking'] is False
    assert 'thinking_budget' not in captured['body']
    assert result.items[0].speaker_reason == 'thinking-off终审'
