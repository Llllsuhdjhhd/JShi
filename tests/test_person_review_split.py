from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from threading import Event
from time import time
from types import SimpleNamespace

from jshi.app.voice import VoiceConversation
from jshi.core.envelope import InputEnvelope, InputPart, SceneUtterance, SpeakerEvidence
from jshi.models import ModelResponse
from jshi.models.prompt import build_user
from jshi.skill.cognition import CognitionSkill
from jshi.style.packs import reply_instruction_for
from jshi.voice.jev import BatchItem, VoiceJEV
from jshi.voice.person_review import PersonReviewer
from jshi.voice.volc import Transcript
from tests.test_voice import FakeCloud, Model, process


async def discard(message):
    pass


def test_main_uses_only_current_structured_input_even_with_legacy_envelope_history(process):
    who = SpeakerEvidence('A', 'lux-id', 'lux', 'recognized', 'voiceprint_match')
    old = SceneUtterance('旧录音不再作为新输入', who, 0, 1800, input_id='old')
    new = replace(old, text='本轮新话', input_id='new', start_ms=2000, end_ms=3800)
    e = InputEnvelope('session', 'microphone', (InputPart('text', '本轮新话\n旧入口旁注'),), who,
        utterances=(old, new), current_utterances=(new,), review_context='历史复判内容',
        input_annotations=({'input_id':'new', 'n':'N2', 'direction':'yes'},))
    process.experience('stone', e.text, envelope=e)
    req = process.cognition.requests[-1]
    assert [item['input_id'] for item in req.input_items] == ['new']
    rendered = build_user(replace(req, system_extra='主认知提示'))
    assert '2. lux：本轮新话' in rendered
    assert '对匠石说' in rendered
    assert all(text not in rendered for text in ('旧录音不再作为新输入', '旧入口旁注', '历史复判内容')), rendered
    assert req.input_review_text == ''


def test_cognition_schema_and_persona_instruction_do_not_request_identity_judgment():
    assert 'speaker_judgments' not in CognitionSkill.schema['properties']
    assert 'object_assessment' not in CognitionSkill.schema['properties']
    assert 'next_jev_note' not in CognitionSkill.schema['properties']
    assert '【人物复判与下一轮接续】' not in reply_instruction_for('wood')


def test_ignored_input_enters_pending_scene_once_and_never_next_input(process):
    class Judge(Model):
        def generate(self, req):
            self.requests.append(req)
            text = json.loads(req.input_text)['batch'][0]['text']
            return ModelResponse(model='test', text=json.dumps({'items': [{'n':1,
                'to_jiangshi':'no' if text == '背景广播' else 'yes',
                'relevance':'unrelated' if text == '背景广播' else 'related'}]}))
    async def run():
        c = VoiceConversation(process, 'stone', FakeCloud(), discard, jev=VoiceJEV(Judge()), input_pause_s=.01)
        try:
            await c.accept(Transcript('背景广播', 'A', 0, 1800, True))
            await asyncio.wait_for(c.queue.join(), 3)
            assert not process.cognition.requests
            old = c.scene[-1]
            c._preserve_background((old,))
            pending = process.pending_scene.render('stone')
            assert pending.count('背景广播') == 1
            await c.accept(Transcript('现在问你', 'B', 2000, 3800, True))
            await asyncio.wait_for(c.queue.join(), 3)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            req = process.cognition.requests[-1]
            assert '背景广播' not in req.input_text
            assert [item['text'] for item in req.input_items] == ['现在问你']
            assert '背景广播' in build_user(replace(req, system_extra='主认知提示'))
            assert not req.input_review_text
        finally:
            await c.close()
    asyncio.run(run())


def test_independent_review_gets_current_text_scene_and_candidate_knowledge_without_blocking_main(process):
    release, started = Event(), Event()
    class Slow(Model):
        def generate(self, req):
            self.requests.append(req)
            started.set()
            release.wait(5)
            return ModelResponse(model='test', text=json.dumps({'speaker_judgments':[
                {'n':'N1', 'speaker_pick':'unknown', 'level':'不确定'}], 'next_jev_note':'还需核对'}))
    process.person_review_model = Slow()
    async def run():
        c = VoiceConversation(process, 'stone', FakeCloud(), discard, input_pause_s=.01)
        try:
            who = SpeakerEvidence('A', 'unknown', '声音待定', method='unassigned_audio')
            old = SceneUtterance('独立核对历史', who, 0, 1800, input_id='history', received_at_ms=int(time()*1000)-1)
            c.scene.append(old)
            await c.accept(Transcript('当前人物疑问', 'A', 2000, 3800, True, identity_uncertain=True))
            await asyncio.wait_for(c.queue.join(), 3)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            assert process.cognition.requests
            assert await asyncio.to_thread(started.wait, 1)
            assert not release.is_set()
            request = process.person_review_model.requests[-1]
            assert request.purpose == 'voice_person_review'
            payload = json.loads(request.input_text)
            assert payload['batch_evidence']['本批证据（N编号对应本批序号）'][0]['text'] == '当前人物疑问'
            assert payload['recent_scene'][0]['text'] == '独立核对历史'
            assert 'candidate_knowledge' in payload and 'pending_events' in payload
            assert '独立核对历史' not in build_user(process.cognition.requests[-1])
            release.set()
            await asyncio.wait_for(c.person_review_queue.join(), 3)
            assert c.recent_main_reviews()[0]['next_jev_note'] == ''
        finally:
            release.set()
            await c.close()
    asyncio.run(run())


def test_review_advice_rejects_invalid_candidate_late_manual_change_and_stale_result(process):
    async def run():
        c = VoiceConversation(process, 'stone', FakeCloud(), discard)
        try:
            who = SpeakerEvidence('A', 'unknown', '待定', method='unassigned_audio')
            u = SceneUtterance('称呼线索', who, 0, 1800, input_id='one')
            code = c.assign_code('lux-id', 'lux')
            c.candidate_cache['one'] = [{'object_id':'lux-id', 'source':'recent'}]
            _, rows = c.review_material((u,), (BatchItem(1, to_jiangshi='no', relevance='unrelated'),))
            assert rows  # Even entry-only observations can be reviewed separately.
            snap = {'current':(u,), 'rows':rows, 'queued_at':time(), 'basis_at_ms':0,
                'candidate_codes':{'lux-id':code}, 'review_id':'test'}
            bad = {'speaker_judgments':[{'n':'N1', 'speaker_pick':'P999', 'level':'确定'}], 'next_jev_note':'错误明确结论'}
            c.accept_person_review(bad, snap)
            assert not c.main_reviews
            good = {'speaker_judgments':[{'n':'N1', 'speaker_pick':code, 'level':'确定',
                'semantic_pick':code, 'semantic_reason':'独立语义线索', 'evidence_relation':'semantic_only'}]}
            c.annotations['one'] = replace(who, object_id='lux-id', method='manual_annotation')
            c.accept_person_review(good, snap)
            assert not c.main_reviews
            c.annotations.clear()
            c.accept_person_review(good, {**snap, 'queued_at':time()-121})
            assert not c.main_reviews
            c.accept_person_review(good, snap)
            assert c.main_reviews and c.annotations == {}
            done = c.main_reviews[-1]['completed_at_ms']
            assert not c.recent_main_reviews((replace(u, input_id='next', received_at_ms=done-1),))
            assert c.recent_main_reviews((replace(u, input_id='next', received_at_ms=done+1),))
        finally:
            await c.close()
    asyncio.run(run())


def test_review_failure_does_not_block_response(process):
    class Broken(Model):
        def generate(self, req):
            raise RuntimeError('review unavailable')
    process.person_review_model = Broken()
    async def run():
        c = VoiceConversation(process, 'stone', FakeCloud(), discard, input_pause_s=.01)
        try:
            await c.accept(Transcript('先正常交流', 'A', 0, 1800, True, identity_uncertain=True))
            await asyncio.wait_for(c.queue.join(), 3)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            await asyncio.wait_for(c.person_review_queue.join(), 3)
            assert process.cognition.requests and not c.main_reviews
        finally:
            await c.close()
    asyncio.run(run())


def test_candidate_knowledge_is_separate_bounded_and_does_not_reassign_unknown_history(process):
    who = SpeakerEvidence('A', 'unknown', '待定', method='unassigned_audio')
    u = SceneUtterance('交往线索', who, 0, 1800, input_id='current')
    calls = []
    process.memory = SimpleNamespace(portrait=lambda sid, oid: {
        'subject_id':sid, 'object_id':oid, 'visible_summary':'候选人的性格' * 600})
    def recall(sid, query, *, object_ids, budget_chars):
        calls.append((sid, query, object_ids, budget_chars))
        return (SimpleNamespace(object_id='lux-id', content='候选相处经验', source_event_ids=('event',)),
                SimpleNamespace(object_id='other', content='不能串给此候选', source_event_ids=()))
    process.long_term_experience = SimpleNamespace(recall_experience=recall)
    process.unknown_inputs.append('old', 'temporary', '人工选中的旧线索', 's', 0, 1800)
    process.unknown_inputs.bind(('old',), 'lux-id')
    snapshot = {'current':(u,), 'candidate_codes':{'lux-id':'P1'}, 'payload':{'pending_events':'尚未整理'}}
    req = PersonReviewer(Model(), process).request('stone', snapshot)
    payload = json.loads(req.input_text)
    row = payload['candidate_knowledge'][0]
    assert row['candidate_only'] and row['who'] == 'P1'
    assert len(row['portrait']) <= 2000
    assert row['experience'] == [{'text':'候选相处经验', 'source_ids':['event']}]
    assert row['manual_history'] == [{'input_id':'old', 'text':'人工选中的旧线索'}]
    assert calls == [('stone', '交往线索', ('lux-id',), 2000)]
    assert u.speaker.object_id == 'unknown'
