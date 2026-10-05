from __future__ import annotations

import asyncio
import json
from threading import Event

from jshi.app.voice import VoiceConversation
from jshi.core.envelope import SceneUtterance, SpeakerEvidence
from jshi.models import ModelResponse, ResponseItem, ResponsePlan
from jshi.subject import HistoryKind
from jshi.voice.jev import VoiceJEV
from jshi.voice.volc import Transcript
from tests.test_voice import FakeCloud, Model, process


def test_related_background_kept_unrelated_dropped_and_memory_clean(process):
    class Judge:
        def generate(self, request):
            return ModelResponse(model='test', text=json.dumps({'items': [
                {'n': 1, 'to_jiangshi': 'no', 'relevance': 'related', 'score': .1},
                {'n': 2, 'to_jiangshi': 'yes', 'relevance': 'related', 'score': .9},
                {'n': 3, 'to_jiangshi': 'no', 'relevance': 'unrelated', 'score': .1},
            ]}))

    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send, jev=VoiceJEV(Judge()), input_pause_s=.02)
        try:
            await c.accept(Transcript('下午博物馆关门', 'A', 0, 1800, True))
            await c.accept(Transcript('匠石，那我们该去哪', 'B', 1900, 3800, True))
            await c.accept(Transcript('你的快递在楼下', 'C', 3900, 5500, True))
            await asyncio.wait_for(c.queue.join(), 2)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            text = process.cognition.requests[0].input_text
            assert '下午博物馆关门' in text and '匠石，那我们该去哪' in text
            assert '你的快递在楼下' not in text
            assert len(c.scene) == 3
            facts = process.repository.list_history('stone', HistoryKind.FACT)
            stored = next(row.content['text'] for row in facts if row.event_type == 'external_input')
            assert 'JEV 初判' not in stored and 'P1' not in stored and 'P2' not in stored
        finally:
            await c.close()
    asyncio.run(run())


def test_only_background_does_not_start_main_and_unknown_relevance_is_kept():
    jev = VoiceJEV()
    decision = jev._from_items([{'n': 1, 'to_jiangshi': 'no', 'relevance': 'related'}], [{'n': 1}], '')
    assert decision.action == 'ignore' and decision.items[0].keep
    decision = jev._from_items([{'n': 1, 'to_jiangshi': 'no'}, {'n': 2, 'to_jiangshi': 'maybe', 'score': float('nan')}],
                              [{'n': 1}, {'n': 2}], '')
    assert decision.action == 'respond' and decision.items[0].keep and decision.items[1].score == .5


def test_short_alias_target_is_resolved_before_playback(process):
    class Reply(Model):
        def generate(self, request):
            self.requests.append(request)
            return ModelResponse(model='test', response_plan=ResponsePlan('respond', items=(ResponseItem('verbal', '你好 lux', ('P1',)),)))
    process.cognition = Reply()
    async def run():
        messages = []
        async def send(message): messages.append(message)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, input_pause_s=.02)
        try:
            await c.accept(Transcript('我是 lux', 'A', 0, 1800, True))
            await asyncio.wait_for(c.queue.join(), 2)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            await asyncio.sleep(.02)
            reply = next(message for message in messages if message['type'] == 'reply')
            assert reply['items'][0]['target_ids'] == ('lux-id',)
        finally:
            await c.close()
    asyncio.run(run())


def test_context_does_not_treat_later_playback_as_heard_before_input(process):
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send)
        try:
            who = SpeakerEvidence('A', 'lux-id', 'lux', 'recognized', 'voiceprint_match')
            prior = SceneUtterance('明天去哪', who, 0, 1000, input_id='prior', received_at_ms=10)
            current = SceneUtterance('那下午呢', who, 2000, 3000, input_id='current', received_at_ms=20)
            later = SceneUtterance('后面才说的', who, 4000, 5000, input_id='later', received_at_ms=40)
            c.scene.extend((prior, current, later))
            c.played_history.extend((
                {'who': '匠石', 'basis': '已播出', 'text': '先去古镇', 'targets': ['lux-id'], 'at': 15},
                {'who': '匠石', 'basis': '已播出', 'text': '再去博物馆', 'targets': ['lux-id'], 'at': 30},
            ))
            context, spoken = c._jev_context((current,))
            text = json.dumps(context, ensure_ascii=False)
            assert '明天去哪' in text and '先去古镇' in spoken
            assert '再去博物馆' not in text and '后面才说的' not in text
            assert context[-1]['targets'] == ['lux-id']
        finally:
            await c.close()
    asyncio.run(run())


def test_failed_write_holds_next_turn_until_retry(process):
    attempts = []
    original = process._commit_zone
    def fail_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return False
        return original(*args, **kwargs)
    process._commit_zone = fail_once
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send, input_pause_s=.02)
        try:
            await c.accept(Transcript('我是 lux', 'A', 0, 1800, True))
            await asyncio.wait_for(c.queue.join(), 2)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            await asyncio.wait_for(c.write_queue.join(), 3)
            assert c.failed_write_jobs and process._unwritten[0]['failed']
            await c.stop('测试停播放')
            await c.accept(Transcript('第二轮问题', 'A', 2000, 3800, True))
            await asyncio.wait_for(c.queue.join(), 2)
            await asyncio.sleep(.03)
            assert len(process.cognition.requests) == 1
            await c.retry_failed_writes()
            await asyncio.wait_for(c.turn_queue.join(), 3)
            await asyncio.wait_for(c.write_queue.join(), 3)
            assert len(process.cognition.requests) == 2 and not process._unwritten
        finally:
            await c.close()
    asyncio.run(run())


def test_emergency_interface_suppresses_stale_reply_while_main_busy(process):
    entered, release = Event(), Event()
    class Slow(Model):
        def generate(self, request):
            entered.set()
            assert release.wait(5)
            return super().generate(request)
    process.cognition = Slow()
    dispatched = []
    original_dispatch = process.action_router.dispatch
    def dispatch(**kwargs):
        dispatched.append(kwargs)
        return original_dispatch(**kwargs)
    process.action_router.dispatch = dispatch
    async def run():
        messages = []
        async def send(message): messages.append(message)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, input_pause_s=.02)
        try:
            await c.accept(Transcript('我是 lux', 'A', 0, 1800, True))
            assert await asyncio.to_thread(entered.wait, 2)
            epoch = c.epoch
            assert await c.request_emergency_interrupt('外部紧急事件', expected_epoch=epoch)
            assert not await c.request_emergency_interrupt('旧事件', expected_epoch=epoch)
            release.set()
            await asyncio.wait_for(c.turn_queue.join(), 3)
            await asyncio.sleep(.03)
            assert not any(message['type'] == 'reply' for message in messages)
            assert not dispatched
            assert any(message['type'] == 'stop' for message in messages)
        finally:
            release.set()
            await c.close()
    asyncio.run(run())


def test_contextual_attribution_survives_into_next_jev_without_overwriting_known_identity(process):
    from jshi.voice.jev import BatchItem
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send)
        try:
            code = c.assign_code('lux-id', 'lux')
            unknown = SpeakerEvidence('A', 'pending', '声音归属待定', 'unknown', 'unassigned_audio')
            first = SceneUtterance('那下午呢', unknown, 0, 500, input_id='first', received_at_ms=10)
            c.scene.append(first)
            c.candidate_cache['first'] = [{'object_id': 'lux-id', 'score': .28}]
            items = (BatchItem(1, speaker_pick=code, speaker_level='确定', speaker_score=.75),)
            result = c._apply_attribution([first], items)
            assert result[0].speaker.method == 'context_attribution'
            assert c.annotations['first'].object_id == 'lux-id'
            next_input = SceneUtterance('再问一句', unknown, 1000, 1500, input_id='next', received_at_ms=20)
            context, _ = c._jev_context((next_input,))
            assert 'lux' in context[0]['who'] and '上下文推测' in context[0]['who']
            assert process.profiles.get('lux-id').status == 'confirmed'
            known = SpeakerEvidence('B', 'other-id', '已确认的人', 'recognized', 'manual_annotation')
            utterance = SceneUtterance('你好', known, 2000, 2500, input_id='known')
            c.candidate_cache['known'] = c.candidate_cache['first']
            assert c._apply_attribution([utterance], items)[0].speaker == known
        finally:
            await c.close()
    asyncio.run(run())


def test_unparseable_writer_is_not_treated_as_successful_empty_edit(process):
    from jshi.skill import SkillModelPort, WriteZoneSkill
    from tests.test_voice import envelope
    class Writer(Model):
        def generate(self, request):
            self.requests.append(request)
            text = 'bad writer output' if len(self.requests) <= 2 else '{"rewritten_context":"完整写好的现场"}'
            return ModelResponse(model='writer', text=text)
    writer = Writer()
    process.write_zone = SkillModelPort(WriteZoneSkill(writer), apply_to=('write_zone',))
    incoming, identities = envelope(process)
    process.experience('stone', incoming.text, envelope=incoming, resolved_speaker=identities.candidate(incoming.speaker), defer_write=True)
    job = process.take_deferred_write()
    assert job() is False and len(writer.requests) == 2
    assert process._unwritten[0]['failed']
    assert job() is True and len(writer.requests) == 3
    assert not process._unwritten
