"""JEV gate wall-clock budget: lite timeout → one fallback_models call; never respond."""
from __future__ import annotations

import json

import pytest

from jshi.models import ModelResponse
from jshi.models.routing import ProviderFailure, RoutedModel
from jshi.voice.jev import JEV_GATE_BUDGET_S, VoiceJEV


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

    def __init__(self, clock, cost=3.0):
        self.clock = clock
        self.cost = cost
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        self.clock.advance(self.cost)
        raise ProviderFailure(408, True, 'G · lite', f'请求超时（{self.cost:g}s）')


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


def test_budget_s_and_fallback_models_init_defaults(monkeypatch):
    monkeypatch.delenv('JSHI_API_QWEN_API_KEY', raising=False)
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    gate = VoiceJEV()
    assert gate.budget_s == JEV_GATE_BUDGET_S == 8.0
    assert gate.fallback_models == []


def test_lite_timeout_calls_qwen_once_no_lite_retry(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    lite = LiteTimeout(clock, cost=3.0)
    trap = LiteRetryTrap()
    qwen = QwenOk(clock, cost=1.0)
    primary = RoutedModel(lite)
    primary.replace([lite, trap])
    gate = VoiceJEV(primary, budget_s=8.0, fallback_models=[qwen])
    started = clock()
    result = gate.decide_batch('stone', _batch(), [], {'state': 'completed'})
    elapsed = clock() - started
    assert result.judged and result.action == 'respond'
    assert lite.calls == 1 and trap.calls == 0 and qwen.calls == 1
    assert elapsed <= 8.0


def test_qwen_success_used_after_lite_timeout(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    lite = LiteTimeout(clock, cost=2.0)
    qwen = QwenOk(clock, cost=0.5, text=_ok_batch(why='Qwen兜底人物终审'))
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen])
    result = gate.decide_batch('stone', _batch(), [], {'state': 'completed'})
    assert result.judged and result.action == 'respond' and not result.model_error
    assert result.items[0].speaker_reason == 'Qwen兜底人物终审'
    assert lite.calls == 1 and qwen.calls == 1


@pytest.mark.parametrize('state,expected', [
    ('completed', 'ignore'),
    ('idle', 'ignore'),
    ('playing', 'resume'),
    ('paused', 'resume'),
])
def test_budget_exhausted_holds_without_respond(monkeypatch, state, expected):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    # Lite alone consumes the full budget; Qwen must not be called.
    lite = LiteTimeout(clock, cost=8.0)
    qwen = QwenOk(clock, cost=1.0)
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen])
    started = clock()
    result = gate.decide_batch('stone', _batch(), [], {'state': state})
    elapsed = clock() - started
    assert result.action == expected
    assert not result.judged and result.model_error
    assert result.items[0].to_jiangshi == 'no'
    assert lite.calls == 1 and qwen.calls == 0
    assert elapsed <= 8.0


def test_qwen_failure_after_lite_timeout_holds(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    lite = LiteTimeout(clock, cost=2.0)
    qwen = QwenFail(clock, cost=1.0)
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen])
    result = gate.decide_batch('stone', [{'n': 1, 'text': '你好'}], [], {'state': 'playing'})
    assert result.action == 'resume'
    assert not result.judged and result.model_error
    assert lite.calls == 1 and qwen.calls == 1


def test_total_elapsed_bounded_by_budget_with_fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    lite = LiteTimeout(clock, cost=5.0)
    qwen = QwenOk(clock, cost=2.5)
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen])
    started = clock()
    result = gate.decide_batch('stone', _batch(), [], {'state': 'completed'})
    elapsed = clock() - started
    assert result.judged and qwen.calls == 1
    assert elapsed <= gate.budget_s == 8.0


def test_decide_single_also_uses_budget_and_fallback(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr('jshi.voice.jev.monotonic', clock)
    lite = LiteTimeout(clock, cost=2.0)

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
    gate = VoiceJEV(lite, budget_s=8.0, fallback_models=[qwen])
    decision = gate.decide('stone', '旁人说话', {}, {'state': 'playing'})
    assert decision.action == 'ignore'
    assert lite.calls == 1 and qwen.calls == 1
