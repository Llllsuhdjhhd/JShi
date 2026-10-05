from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from threading import Event
from time import monotonic
from types import SimpleNamespace

import numpy as np
import pytest

from jshi.app.voice import VoiceConversation
from jshi.core.conversation_review import parse_review, parse_reply_targets
from jshi.core.envelope import InputEnvelope, InputPart, SceneUtterance, SpeakerEvidence
from jshi.models import ModelResponse, ResponseItem, ResponsePlan
from jshi.recognition import ObjectProfile
from jshi.skill.cognition import _persona_to_model_response
from jshi.voice.jev import VoiceJEV, explicit_name
from jshi.voice.volc import Transcript
from tests.test_voice import FakeCloud, Model, process


async def discard(message):
    pass


@pytest.mark.parametrize('physical,semantic,relation,expected,level', [
    ({'pick': 'P1', 'strength': 'clear'}, 'P1', 'agree', 'agree', '确定'),
    ({'pick': 'P1', 'strength': 'weak'}, 'P2', 'semantic_only', 'semantic_only', '确定'),
    ({'pick': 'P1', 'strength': 'clear'}, 'unknown', 'voice_only', 'voice_only', '确定'),
    ({'pick': 'P1', 'strength': 'clear'}, 'P2', 'agree', 'conflict', '可能'),
    ({'pick': '', 'strength': 'unavailable'}, 'unknown', 'agree', 'insufficient', '不确定'),
])
def test_jev_combines_physical_and_semantic_support(physical, semantic, relation, expected, level):
    batch = [{'n': 1, 'voice_evidence': physical, 'candidates': [{'who': 'P1'}, {'who': 'P2'}]}]
    raw = [{'n': 1, 'to_jiangshi': 'no', 'relevance': 'related', 'speaker': {
        'pick': 'P2' if semantic == 'P2' else 'P1', 'level': '确定', 'score': .8,
        'semantic_pick': semantic, 'semantic_reason': '原话接续', 'evidence_relation': relation}}]
    decision = VoiceJEV()._from_items(raw, batch, '')
    item = decision.items[0]
    assert item.voice_pick == physical['pick'] and item.voice_strength == physical['strength']
    assert item.semantic_pick == semantic and item.semantic_reason == '原话接续'
    assert item.evidence_relation == expected and item.speaker_level == level
    if expected == 'conflict':
        assert decision.needs_main_review and decision.action == 'respond'


def test_physical_support_is_not_previous_guess_or_self_introduction(process):
    async def run():
        c = VoiceConversation(process, 'stone', FakeCloud(), discard)
        try:
            c.local_speakers = SimpleNamespace(threshold=.65, margin=.08)
            who = SpeakerEvidence('A', 'lux-id', 'lux', 'introduced', 'self_introduction')
            u = SceneUtterance('你好', who, 0, 1800, input_id='input')
            assert c.voice_evidence(u, [{'who': 'P1', 'score': .9, 'source': 'recent'}])['strength'] == 'unavailable'
            voice = c.voice_evidence(u, [{'who': 'P1', 'score': .7, 'source': 'voiceprint'}, {'who': 'P2', 'score': .69, 'source': 'voiceprint'}])
            assert voice['strength'] == 'weak'
            voice = c.voice_evidence(u, [{'who': 'P1', 'score': .7, 'source': 'voiceprint'}, {'who': 'P2', 'score': .5, 'source': 'voiceprint'}])
            assert voice['strength'] == 'clear' and voice['pick'] == 'P1'
            c.source_speakers[u.input_id] = replace(who, status='unknown', method='unassigned_audio')
            assert c.voice_evidence(replace(u, speaker=replace(who, method='context_attribution')), [])['strength'] == 'unavailable'
        finally:
            await c.close()
    asyncio.run(run())


def test_main_review_cannot_confirm_unresolved_evidence_conflict():
    parsed = parse_review({'speaker_judgments': [{'n': 'N1', 'speaker_pick': 'P2', 'level': '确定',
        'semantic_pick': 'P2', 'semantic_reason': '原话接续', 'evidence_relation': 'conflict'}]})
    assert parsed['speaker_judgments'][0]['level'] == '可能'
    assert parsed['speaker_judgments'][0]['evidence_relation'] == 'conflict'


def test_main_can_withdraw_jev_guess_and_batch_request_is_visible(process):
    async def run():
        c = VoiceConversation(process, 'stone', FakeCloud(), discard)
        try:
            code = c.assign_code('lux-id', 'lux')
            raw = SpeakerEvidence('A', 'unknown', '声音归属待定', method='unassigned_audio')
            guess = replace(raw, object_id='lux-id', label='lux', status='provisional', method='context_attribution')
            u = SceneUtterance('那接着说', guess, 0, 800, input_id='one')
            c.source_speakers[u.input_id] = raw
            c.candidate_cache[u.input_id] = [{'object_id': 'lux-id', 'source': 'recent'}]
            c.scene.append(u)
            context, rows = c.review_material((u,), (), needs_main_review=True)
            assert json.loads(context)['本批复判请求（非逐条开关）'] is True
            envelope = InputEnvelope(c.session_id, 'voice', (InputPart('text', c.render_input((u,))),), guess,
                current_utterances=(u,), utterances=(u,), review_context=context, review_candidates=rows)
            response = ModelResponse(model='test', speaker_judgments=({'n': 'N1', 'speaker_pick': 'unknown',
                'level': '不确定', 'evidence': '接话也可能来自旁人', 'to_jiangshi': 'maybe'},))
            final, updated, _ = c.apply_main_review(response, envelope, c.epoch)
            assert updated.current_utterances[0].speaker == raw
            assert 'lux' not in updated.text and '待定声音' in updated.text
            assert final.speaker_judgments[0]['to_jiangshi'] == 'maybe'
            protected = replace(u, speaker=replace(guess, status='recognized', method='voiceprint_match'))
            protected_envelope = replace(envelope, speaker=protected.speaker, current_utterances=(protected,), utterances=(protected,))
            _, updated, _ = c.apply_main_review(response, protected_envelope, c.epoch)
            assert updated.current_utterances[0].speaker == protected.speaker
        finally:
            await c.close()
    asyncio.run(run())


def test_review_parser_and_persona_keep_short_reason_and_targets():
    judgment = {'n': 'N1', 'speaker_pick': 'P1', 'level': '可能', 'score': .6,
                'evidence': '接续同一话题', 'to_jiangshi': 'maybe', 'address_reason': '没有直接称呼'}
    parsed = parse_review({'speaker_judgments': [judgment, {**judgment, 'speaker_pick': 'P2'}], 'next_jev_note': '未确认' * 80})
    assert len(parsed['speaker_judgments']) == 1
    assert len(parsed['next_jev_note']) == 120
    assert parse_reply_targets({'reply_targets': ['P1', 'S1', 'P1', 'invented']}) == ('P1', 'S1')
    response = _persona_to_model_response({'mode': 'respond', 'reply': '你好', 'action': '无动作',
        'speaker_judgments': [judgment], 'reply_targets': ['S1'], 'next_jev_note': 'S1可能是P1，接续上一句。'}, 'test')
    assert response.speaker_judgments[0]['speaker_pick'] == 'P1'
    assert response.response_plan.items[0].target_ids == ('S1',)
    assert response.next_jev_note.startswith('S1可能')


def test_names_and_subject_nicknames_are_portrait_material_without_merging(process):
    async def run():
        c = VoiceConversation(process, 'stone', FakeCloud(), discard)
        try:
            known, _ = c.identities.introduce('A', 'lux')
            same, _ = c.identities.introduce('A', '小鹿')
            assert same.object_id == known.object_id == 'lux-id'
            c.note_person_names(known, '大家都叫我小琴', 'alias')
            c.note_address(known, '我以后就叫你姜哥', 'term', addressed=True)
            portrait = process.profiles.address_line('lux-id')
            assert all(name in portrait for name in ('小鹿', '小琴', '姜哥', '别人称此人为'))
            process.profiles.create(ObjectProfile('other', '小琴', status='confirmed'))
            assert {p.object_id for p in process.profiles.find_by_names('小琴')} == {'lux-id', 'other'}
            unknown, note = c.identities.introduce('B', '小琴', uncertain=True)
            assert unknown.object_id not in {'lux-id', 'other'} and '多个' in note
            conflict, note = c.identities.introduce('A', '陌生名')
            process.profiles.create(ObjectProfile('third', '另一人', status='confirmed'))
            conflict, note = c.identities.introduce('A', '另一人')
            assert conflict.object_id == 'lux-id' and '冲突' in note
            assert '另一人' not in process.profiles.get('lux-id').aliases
            assert explicit_name('我叫你姜哥') == ''
        finally:
            await c.close()
    asyncio.run(run())


def test_candidates_use_normalized_audio_budget_and_reuse(process):
    async def run():
        c = VoiceConversation(process, 'stone', FakeCloud(), discard, candidate_timeout_s=.02)
        release = Event()
        captured = []
        try:
            t = Transcript('你好', 'A', 0, 1800, True, input_id='audio')
            who = SpeakerEvidence('A', 'unassigned', '声音归属待定', method='unassigned_audio')
            c.input_records[t.input_id] = {'pcm': np.full(24000, 16384, dtype='<i2').tobytes()}
            def rank(samples):
                captured.append(samples)
                release.wait(1)
                return []
            c.local_speakers = SimpleNamespace(rank_known=rank)
            start = monotonic()
            _, status = await c.rank_candidates(t, who)
            assert status == 'candidate_timeout' and monotonic() - start < .3
            assert captured[0].dtype == np.float32 and np.allclose(captured[0], .5)
            _, status = await c.rank_candidates(t, who)
            assert status == 'previous_candidate_still_running'
            reused = replace(t, identity_note=json.dumps({'candidates': [{'object_id': 'lux-id', 'score': .28}]}))
            candidates, status = await c.rank_candidates(reused, who)
            assert status == 'reused_voiceprint_candidates' and candidates[0]['score'] == .28
            assert len(captured) == 1
            c.input_records[t.input_id]['pcm'] = b'\x00' * 1000
            _, status = await c.rank_candidates(t, who)
            assert status == 'short_audio'
        finally:
            release.set()
            await c.close()
    asyncio.run(run())


def test_main_review_distinguishes_two_unassigned_lines_and_passes_feedback(process):
    async def run():
        c = VoiceConversation(process, 'stone', FakeCloud(), discard)
        try:
            process.profiles.create(ObjectProfile('other', 'lux', status='confirmed'))
            p1 = c.assign_code('lux-id', 'lux')
            p2 = c.assign_code('other', 'lux')
            source = SpeakerEvidence('A', 'unknown', '声音归属待定', method='unassigned_audio')
            one = SceneUtterance('我接着说上午的事', source, 0, 800, input_id='one', received_at_ms=10)
            two = SceneUtterance('我问的是下午', source, 900, 1700, input_id='two', received_at_ms=11)
            prior = SceneUtterance('咱们下午换个地方', source, 0, 700, input_id='prior', received_at_ms=5)
            c.scene.extend((prior, one, two))
            for u in (one, two):
                c.candidate_cache[u.input_id] = [{'object_id': 'lux-id', 'score': .28}, {'object_id': 'other', 'score': .2}]
            context, rows = c.review_material((one, two), ())
            assert '咱们下午换个地方' not in context
            assert json.loads(context)['本批证据（N编号对应本批序号）'][0]['candidate_portraits'][0]['candidate_only']
            envelope = InputEnvelope(c.session_id, 'voice', (InputPart('text', c.render_input((one, two))),), source,
                current_utterances=(one, two), utterances=tuple(c.scene), review_context=context, review_candidates=rows)
            judgments = tuple({'n': n, 'speaker_pick': p, 'level': '确定', 'score': .8, 'evidence': '原话接续',
                'to_jiangshi': 'yes', 'address_reason': '直接提问'} for n, p in (('N1', p1), ('N2', p2)))
            response = ModelResponse(model='test', response_plan=ResponsePlan('respond', items=(ResponseItem('verbal', '下午再说', (p2,)),)),
                speaker_judgments=judgments, next_jev_note='两句分别接续不同话题，仍是上下文归属。')
            final, updated, actor = c.apply_main_review(response, envelope, c.epoch)
            assert [u.speaker.object_id for u in updated.current_utterances] == ['lux-id', 'other']
            assert updated.text.splitlines()[0].startswith(f'1. {p1}（lux，上下文推测）')
            assert updated.text.splitlines()[1].startswith(f'2. {p2}（lux，上下文推测）')
            assert final.response_plan.items[0].target_ids == ('other',)
            next_u = replace(one, input_id='next', received_at_ms=12)
            feedback = c.recent_main_reviews((next_u,))
            assert feedback[0]['next_jev_note'] == response.next_jev_note
            assert '非声纹确认' in feedback[0]['source']
            assert not c.recent_main_reviews((replace(next_u, received_at_ms=9),))
            before = len(c.main_reviews)
            c.apply_main_review(response, envelope, c.epoch + 1)
            assert len(c.main_reviews) == before
            invalid = replace(response, speaker_judgments=({**judgments[0], 'speaker_pick': 'P999'},), next_jev_note='')
            ignored, unchanged, _ = c.apply_main_review(invalid, envelope, c.epoch)
            assert not ignored.speaker_judgments and unchanged.current_utterances[0].speaker == source
        finally:
            await c.close()
    asyncio.run(run())


def test_review_request_can_open_related_batch_without_forcing_speech():
    decision = VoiceJEV()._from_items([{'n': 1, 'to_jiangshi': 'no', 'relevance': 'related'}], [{'n': 1}], '', needs_main_review=True)
    assert decision.action == 'respond' and decision.needs_main_review
    decision = VoiceJEV()._from_items([{'n': 1, 'to_jiangshi': 'no', 'relevance': 'unrelated'}], [{'n': 1}], '', needs_main_review=True)
    assert decision.action == 'ignore'


def test_main_review_is_applied_before_write_and_next_jev_receives_it(process):
    calls = []
    written = []
    class Judge:
        def generate(self, request):
            calls.append(request.input_text)
            return ModelResponse(model='test', text=json.dumps({'items': [{'n': 1, 'to_jiangshi': 'yes', 'relevance': 'related'}]}))
    class Main(Model):
        def generate(self, request):
            self.requests.append(request)
            if len(self.requests) != 2:
                return ModelResponse(model='test', text='听到了。')
            assert '前文' not in request.input_review_text and '我是 lux' not in request.input_review_text
            assert '我还要接着说' in request.input_text
            return ModelResponse(model='test', response_plan=ResponsePlan('respond', items=(ResponseItem('verbal', '继续说吧', ('P1',)),)),
                speaker_judgments=({'n': 'N1', 'speaker_pick': 'P1', 'level': '确定', 'score': .8,
                    'evidence': '明确接续原话', 'to_jiangshi': 'yes', 'address_reason': '接着对匠石说'},),
                next_jev_note='这句接续lux的原话，归属来自上下文，未确认声纹。')
    original = process._write_zone
    def write(subject_id, activity, current, *args, **kwargs):
        written.append(current.input_text)
        return original(subject_id, activity, current, *args, **kwargs)
    process._write_zone = write
    writer = Model()
    process.write_zone = writer
    process.cognition = Main()
    async def run():
        messages = []
        async def send(message): messages.append(message)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, jev=VoiceJEV(Judge()), input_pause_s=.01)
        try:
            async def finish():
                await asyncio.wait_for(c.queue.join(), 3)
                await asyncio.wait_for(c.turn_queue.join(), 3)
                await asyncio.wait_for(c.write_queue.join(), 3)
            await c.accept(Transcript('我是 lux', 'A', 0, 1800, True))
            await finish()
            await c.accept(Transcript('我还要接着说', 'B', 2000, 2800, True, identity_uncertain=True))
            await finish()
            assert 'lux（上下文推测）' in written[1]
            response = next(message for message in messages if message['type'] == 'reply' and '继续说吧' in message['text'])
            assert response['items'][0]['target_ids'] == ('lux-id',)
            from jshi.models.prompt import build_user
            assert '前文与本批证据' not in build_user(writer.requests[1])
            await c.accept(Transcript('下一句', 'C', 3000, 3800, True, identity_uncertain=True))
            await finish()
            assert '归属来自上下文，未确认声纹' in calls[-1]
            assert 'input_worker_wait_ms' in next(iter(c.voice_timings.values()))[1]
        finally:
            await c.close()
    asyncio.run(run())
