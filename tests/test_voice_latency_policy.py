import asyncio
import json
from time import monotonic, sleep
from dataclasses import replace
from threading import Event
from urllib.error import HTTPError

import pytest

from jshi.app.voice import VoiceConversation
from jshi.assembly.port import AssemblyContext, AssemblySpeaker
from jshi.assembly.sources import MemorySource
from jshi.core.envelope import InputEnvelope, SceneUtterance, SpeakerEvidence
from jshi.core.prompt_profile import PromptProfile, select_profile
from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV, BatchItem
from jshi.voice.jev_scene import JEVSceneStore, JEVSceneWriter
from jshi.voice.volc import Transcript
from tests.test_voice import process, FakeCloud, Model


def test_overlapping_batch_is_not_dropped_or_used_to_confirm_identity():
    class Judge:
        calls=0
        def generate(self,r):
            self.calls+=1
            payload=json.loads(r.input_text)
            assert payload['input']['lines'][0]['overlap'] is True
            assert payload['input']['lines'][0]['p']=='unknown'
            assert 'candidates' not in payload['input']['lines'][0]
            return ModelResponse(text=json.dumps({'items':[{'i':1,'person':'unknown','certainty':'unknown','why':'重叠声音身份不能确认'}],'level':1,'recall_memory':False}),model='fake')
    judge=Judge()
    batch=[{'n':1,'text':'匠石为什么没有回应','who':'P1','candidates':[{'who':'P1','score':.9}]}]
    result=VoiceJEV(judge).decide_batch('stone',batch,[],{'state':'playing'},overlap=True)
    assert judge.calls==1 and result.action=='respond' and all(i.keep and i.identity_only for i in result.items)
    assert result.items[0].speaker_pick=='unknown'
    fallback=VoiceJEV().decide_batch('stone',batch,[],{'state':'idle'},overlap=True)
    assert fallback.action=='respond' and all(i.keep for i in fallback.items)


def test_visual_snapshot_wait_keeps_voice_event_loop_responsive(process):
    from types import SimpleNamespace
    from jshi.core.envelope import InputPart
    entered, release = Event(), Event()
    def delayed(subject):
        entered.set()
        assert release.wait(3)
        return None
    process.vision = SimpleNamespace(store=SimpleNamespace(latest_sample=delayed))
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send)
        speaker = SpeakerEvidence('A')
        u = SceneUtterance('匠石，你听得到吗', speaker, 0, 2000, input_id='direct')
        e = InputEnvelope('test', 'voice', (InputPart('text', u.text),), speaker, current_utterances=(u,))
        task = asyncio.create_task(c._judge((u,), speaker, False, e))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            # An event-loop timer must fire while SQLite still waits in a worker.
            await asyncio.wait_for(asyncio.sleep(.01), .5)
            assert not task.done()
        finally:
            release.set()
            await task
            await c.close()
    asyncio.run(run())


def test_failed_jev_keeps_tools_without_speculative_history_search():
    for failure in ({'timed_out': True}, {'model_error': 'HTTP 402'}, {'model_error': 'invalid JSON'}):
        assert select_profile(['a'], [{'input_ids': ['a'], **failure}]) == PromptProfile(1, recall_memory=False)
    assert select_profile(['a'], [{'input_ids': ['old'], 'model_error': 'HTTP 402'}]) == PromptProfile()
    assert select_profile(['a'], []) == PromptProfile()


def test_main_thinking_is_not_playback_for_rule_fallback():
    assert VoiceJEV().decide('stone', '我再补充一句', {}, {'state': 'thinking'}).action == 'respond'
    assert VoiceJEV().decide('stone', '我再补充一句', {}, {'state': 'playing'}).action == 'resume'


def test_selected_memory_can_finish_after_1_5_seconds(process):
    class Memory:
        calls = 0
        def recall(self, *args, **kwargs):
            self.calls += 1
            sleep(1.8)
            return ()
    memory = Memory()
    source = MemorySource(process.repository, memory)
    ctx = AssemblyContext('stone', '还记得昨天吗', AssemblySpeaker('lux-id', 'lux'))
    start = monotonic()
    assert source.load(ctx).fragments == ()
    assert monotonic() - start >= 1.7
    assert memory.calls == 1
    assert source.load(replace(ctx, recall_enabled=False)).fragments == ()
    assert memory.calls == 1


def test_ready_speech_group_can_start_main_while_more_speech_keeps_arriving(process):
    entered = Event()
    class Main(Model):
        def generate(self, request):
            entered.set()
            return super().generate(request)
    process.cognition = Main()
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send, input_pause_s=.06, input_max_batch_s=.08)
        stop = asyncio.Event()
        async def producer():
            n = 0
            while not stop.is_set():
                await c.accept(Transcript(f'继续说第{n}句话', 'A', n*1000, n*1000+900, True))
                n += 1
                await asyncio.sleep(.01)
        task = asyncio.create_task(producer())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert not task.done()  # continuous input no longer prevents a ready turn
        finally:
            stop.set()
            await task
            await asyncio.wait_for(c.queue.join(), 3)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            timing = c.last_debug['timing']['voice']
            assert timing['input_timings']
            assert timing['current_jev_ms'] + timing['historical_jev_ms'] == timing['jev_ms']
            await c.close()
    asyncio.run(run())


def test_other_track_partial_speech_does_not_extend_completed_speaker_wait(process):
    entered = Event()
    class Main(Model):
        def generate(self, request):
            if '匠石，你听得到吗' in request.input_text:
                entered.set()
            return super().generate(request)
    process.cognition = Main()
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send,
            input_pause_s=.08, input_max_batch_s=.8)
        stop = asyncio.Event()
        async def background():
            await asyncio.sleep(.01)
            await c.accept(Transcript('旁人正在聊天', 'B', 2100, 2900, True))
            while not stop.is_set():
                await c.accept(Transcript('还在说', 'B', 3000, 4000, False))
                await asyncio.sleep(.01)
        try:
            await c.accept(Transcript('匠石，你听得到吗', 'A', 0, 2000, True))
            task = asyncio.create_task(background())
            assert await asyncio.to_thread(entered.wait, .5)
            assert not task.done()
            assert any(u.text == '旁人正在聊天' for u in c.scene)
        finally:
            stop.set()
            if 'task' in locals(): await task
            await asyncio.wait_for(c.queue.join(), 2)
            await asyncio.wait_for(c.turn_queue.join(), 2)
            await c.close()
    asyncio.run(run())


def test_unfinished_anchor_still_waits_despite_other_completed_speech(process):
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send, input_pause_s=.04)
        try:
            await c.accept(Transcript('还有一种问题是', 'A', 0, 2000, True))
            await asyncio.sleep(.02)
            await c.accept(Transcript('旁人说一句完整的话', 'B', 2100, 2900, True))
            await asyncio.sleep(.2)
            assert not process.cognition.requests
            await c.accept(Transcript('怎么把声音接上名字', 'A', 3000, 4500, True))
            await asyncio.wait_for(c.queue.join(), 1)
            await asyncio.wait_for(c.turn_queue.join(), 2)
            assert len(process.cognition.requests) == 1
            assert all(text in process.cognition.requests[0].input_text for text in (
                '还有一种问题是', '旁人说一句完整的话', '怎么把声音接上名字'))
        finally:
            await c.close()
    asyncio.run(run())


def test_voice_collection_limit_can_be_configured_without_api_key(monkeypatch):
    from jshi.voice.config import VoiceConfig
    monkeypatch.delenv('JSHI_VOICE_INPUT_MAX_BATCH_MS', raising=False)
    assert VoiceConfig.from_env(asr='local', tts='local').input_max_batch_s == 6
    monkeypatch.setenv('JSHI_VOICE_INPUT_MAX_BATCH_MS', '1800')
    assert VoiceConfig.from_env(asr='local', tts='local').input_max_batch_s == 1.8
    monkeypatch.setenv('JSHI_VOICE_INPUT_MAX_BATCH_MS', '0')
    with pytest.raises(ValueError):
        VoiceConfig.from_env(asr='local', tts='local')


def test_unresolved_segment_labels_are_not_assumed_to_identify_one_voice():
    for track in ('pending-0', 'unidentified-1000', 'overlap-2000-0'):
        assert VoiceConversation.speech_group_key(Transcript('测试',track,0,1000,True)) is None


def test_same_acoustic_identity_partial_continuation_survives_track_change(process):
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send, input_pause_s=.12)
        try:
            await c.accept(Transcript('你好', 'A', 0, 1000, True, voiceprint_id='same-voice'))
            await asyncio.sleep(.08)
            await c.accept(Transcript('我想', 'B', 1100, 1800, False, voiceprint_id='same-voice'))
            await asyncio.sleep(.08)
            assert not process.cognition.requests
            await c.accept(Transcript('我想问一件事', 'B', 1100, 2500, True, voiceprint_id='same-voice'))
            await asyncio.wait_for(c.queue.join(), 1)
            await asyncio.wait_for(c.turn_queue.join(), 2)
            assert len(process.cognition.requests) == 1
            assert '我想问一件事' in process.cognition.requests[0].input_text
        finally:
            await c.close()
    asyncio.run(run())


def test_safety_batch_boundary_is_not_reported_as_end_of_expression(process):
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send,
            input_pause_s=.3, input_max_batch_s=.04)
        try:
            await c.accept(Transcript('说到这里', 'A', 0, 2000, True))
            await asyncio.wait_for(c.queue.join(), 1)
            await asyncio.wait_for(c.turn_queue.join(), 2)
            assert c.last_debug['timing']['voice']['jev_calls'][-1]['collection_complete'] is False
            assert '不能据此认为表达已经结束' in process.cognition.requests[0].input_text
        finally:
            await c.close()
    asyncio.run(run())


def test_coalesced_judgments_follow_input_ids_not_last_batch_positions():
    who = SpeakerEvidence('A')
    a = SceneUtterance('前一句', who, 0, 1000, input_id='a')
    b = SceneUtterance('后一句', who, 1000, 2000, input_id='b')
    envelope = InputEnvelope('session', 'microphone', (), who, current_utterances=(a, b), jev_calls=(
        {'input_ids': ['a'], 'items': [dict(n=1, to_jiangshi='yes', speaker_pick='P1')]},
        {'input_ids': ['b'], 'items': [dict(n=1, to_jiangshi='no', speaker_pick='P2', relevance='unrelated')]},
    ))
    items = VoiceConversation.envelope_items(envelope)
    assert [(i.n, i.speaker_pick, i.keep) for i in items] == [(1, 'P1', True), (2, 'P2', False)]


def test_402_cooldown_preserves_failure_and_resumes_after_interval(monkeypatch, tmp_path):
    now = [100.0]
    monkeypatch.setattr('jshi.voice.jev.monotonic', lambda: now[0])
    monkeypatch.setattr('jshi.voice.jev_scene.monotonic', lambda: now[0])
    class Model:
        calls = 0
        def generate(self, request):
            self.calls += 1
            raise HTTPError('https://example.invalid', 402, 'Payment Required', None, None)
    model = Model()
    gate = VoiceJEV(model)
    batch = [{'n': 1, 'text': '你好'}]
    assert '402' in gate.decide_batch('stone', batch, [], {}).model_error
    assert '冷却' in gate.decide_batch('stone', batch, [], {}).model_error
    assert model.calls == 1
    now[0] += 61
    gate.decide_batch('stone', batch, [], {})
    assert model.calls == 2
    writer = JEVSceneWriter(model)
    store = JEVSceneStore(tmp_path / 'scene.json')
    store.append('a', {'text': '你好'})
    with pytest.raises(HTTPError):
        writer.run('stone', store.snapshot(), '')
    with pytest.raises(RuntimeError, match='保留待办'):
        writer.run('stone', store.snapshot(), '')
    assert model.calls == 3 and len(store.snapshot()['events']) == 1


def test_invalid_prompt_routing_not_reported_as_successful_jev():
    class Model:
        def generate(self, request):
            return ModelResponse(model='fake', text=json.dumps({'level': 9, 'recall_memory': False,
                'items': [{'i': 1, 'person': 'unknown', 'certainty': 'unknown', 'to': 'yes', 'keep': 'related'}]}))
    result = VoiceJEV(Model()).decide_batch('stone', [{'n': 1, 'text': '你好'}], [], {})
    assert not result.judged and result.model_error and not result.main_prompt_hint
