"""JEV/ASR unavailable must not fail-open into respond (v0_033 / v0_042 / v0_043)."""
from __future__ import annotations

import asyncio
import json
from urllib.error import HTTPError

import pytest

from jshi.app.voice import VoiceConversation
from jshi.core.envelope import SceneUtterance, SpeakerEvidence
from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV
from jshi.voice.volc import Transcript
from tests.test_voice import FakeCloud, process


class BrokenJEV:
    def generate(self, request):
        raise RuntimeError('JEV upstream unavailable')


class GarbageASRModel:
    """ASR-garbage / unparseable model output → same unavailable path as v0_042/v0_043."""

    def generate(self, request):
        return ModelResponse(model='fake', text='<<<ASR GARBAGE@@@### not json')


class InvalidSchemaModel:
    def generate(self, request):
        return ModelResponse(model='fake', text=json.dumps({
            'items': [{'i': 1, 'person': 'P999', 'certainty': 'confirmed', 'why': '幻觉人物'}],
            'level': 3, 'recall_memory': False, 'main_image': 'omit', 'route': 'main', 'reply': ''}))


@pytest.mark.parametrize('state,expected', [
    ('completed', 'ignore'),
    ('thinking', 'ignore'),
    ('idle', 'ignore'),
    ('', 'ignore'),
    ('playing', 'resume'),
    ('paused', 'resume'),
    ('queued', 'resume'),
])
def test_v0_033_batch_unavailable_does_not_fail_open_to_respond(state, expected):
    """Gold for v0_033-style unavailable: ignore/wait (or resume while playing), never respond."""
    batch = [{'n': 1, 'text': '匠石你在吗', 'who': 'unknown'}]
    delivery = {'state': state} if state else {}
    result = VoiceJEV(BrokenJEV()).decide_batch('stone', batch, [], delivery)
    assert result.action == expected
    assert not result.judged and result.model_error
    assert result.items[0].to_jiangshi == 'no' and not result.items[0].keep


@pytest.mark.parametrize('model_cls', [GarbageASRModel, InvalidSchemaModel])
def test_v0_042_043_asr_garbage_and_invalid_jev_wait_instead_of_respond(model_cls):
    result = VoiceJEV(model_cls()).decide_batch(
        'stone', [{'n': 1, 'text': 'T THE WEST LIN AND THE ELEPANT'}], [], {'state': 'completed'})
    assert result.action == 'ignore'
    assert not result.judged and result.model_error
    assert result.items[0].to_jiangshi == 'no'


def test_402_cooldown_also_holds_without_respond(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('jshi.voice.jev.monotonic', lambda: now[0])

    class Model:
        calls = 0

        def generate(self, request):
            self.calls += 1
            raise HTTPError('https://example.invalid', 402, 'Payment Required', None, None)

    gate = VoiceJEV(Model())
    batch = [{'n': 1, 'text': '你好'}]
    first = gate.decide_batch('stone', batch, [], {'state': 'completed'})
    cooled = gate.decide_batch('stone', batch, [], {'state': 'completed'})
    assert first.action == 'ignore' and cooled.action == 'ignore'
    assert '402' in first.model_error and '冷却' in cooled.model_error
    playing = gate.decide_batch('stone', batch, [], {'state': 'playing'})
    assert playing.action == 'resume'


def test_single_decide_unavailable_matches_batch_hold_policy():
    broken = BrokenJEV()
    assert VoiceJEV(broken).decide('stone', '旁人说话', {}, {'state': 'playing'}).action == 'resume'
    assert VoiceJEV(broken).decide('stone', '旁人说话', {}, {'state': 'completed'}).action == 'ignore'


def test_successful_entry_still_reaches_respond_for_should_answer_recall():
    """Do not regress respond recall: valid JEV judgment must still enter main path."""
    class Judge:
        def generate(self, request):
            return ModelResponse(model='fake', text=json.dumps({
                'items': [{'i': 1, 'person': 'P1', 'certainty': 'confirmed', 'why': '明确对匠石提问'}],
                'level': 2, 'recall_memory': False, 'main_image': 'omit', 'route': 'main', 'reply': ''}))

    result = VoiceJEV(Judge()).decide_batch(
        'stone', [{'n': 1, 'text': '匠石，今天天气怎么样？', 'who': 'P1（lux）',
                   'voice_evidence': {'pick': 'P1', 'strength': 'clear'},
                   'candidates': [{'who': 'P1', 'score': .8}]}],
        [], {'state': 'completed'})
    assert result.judged and result.action == 'respond' and not result.model_error


def test_conversation_unavailable_does_not_enqueue_main_turn(process):
    """Production voice path: unavailable → ignore/resume, never persona respond."""
    async def run():
        messages = []

        async def send(message):
            messages.append(message)

        c = VoiceConversation(process, 'stone', FakeCloud(), send,
                              jev=VoiceJEV(BrokenJEV()), input_pause_s=.01)
        turns = []
        c.enqueue_turn = lambda envelope: turns.append(envelope)  # type: ignore[method-assign]
        try:
            await c.accept(Transcript('T THE WEST', 'noise', 0, 1200, True))
            await asyncio.wait_for(c.queue.join(), 3)
            assert turns == []
            jev = [m for m in messages if m.get('type') == 'jev']
            assert jev and jev[-1]['action'] == 'ignore'
            notice = [m for m in messages if m.get('type') == 'notice']
            assert any('等待' in m.get('text', '') or '不开口' in m.get('text', '') for m in notice)
        finally:
            await c.close()

    asyncio.run(run())


def test_judge_timeout_open_batch_holds_without_respond(process):
    async def run():
        class Slow:
            def decide_batch(self, *args, **kwargs):
                import time
                time.sleep(2)
                raise AssertionError('should have timed out')

        async def send(message):
            pass

        c = VoiceConversation(process, 'stone', FakeCloud(), send, jev=Slow(), timeout_s=.05)
        try:
            u = SceneUtterance('你好匠石', SpeakerEvidence('A'), 0, 1000, input_id='t1')
            decision, record, items = await c._judge((u,), u.speaker, False)
            assert decision.action == 'ignore'
            assert record['timed_out'] and record['action'] == 'ignore'
            assert items and items[0].to_jiangshi == 'no'
        finally:
            await c.close()

    asyncio.run(run())
