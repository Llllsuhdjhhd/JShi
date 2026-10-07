import json
import math
import numpy as np
import pytest

from jshi.voice.references import score, references, VoiceCandidates
from jshi.voice.local import LocalSpeakers
from jshi.voice.interaction import InteractionFeedback
from tests.test_voice import process


def vector(angle):
    return [math.cos(angle), math.sin(angle)]


def test_mean_ranks_high_match_above_low_zero_variance():
    high = {'references': [{'embedding': vector(a)} for a in (.4, .55, .7)]}
    low = {'embedding': vector(1.3)}
    assert score([1., 0.], high)['score'] > score([1., 0.], low)['score']
    assert score([1., 0.], low)['stddev'] is None
    assert score([1., 0.], high)['reference_count'] == 3


def test_quality_weights_not_reference_count_or_best_score():
    entry = {'references': [{'embedding': [1.,0.], 'quality': .25},
                            {'embedding': [0.,1.], 'quality': .75}]}
    assert score([1.,0.], entry)['score'] == pytest.approx(.25)
    entry['references'] *= 5
    assert score([1.,0.], entry)['score'] == pytest.approx(.25)
    assert score([1.,0.], entry)['reference_count'] == 2


def test_manual_independent_references_survive_restart_and_single_input_compares_immediately(process, tmp_path):
    bank = LocalSpeakers(None, 'test', tmp_path/'bank.json', process.profiles)
    samples = iter([vector(.1), vector(.4), vector(.25)])
    bank.embedding = lambda _: next(samples)
    stats = bank.enroll_samples([bytes(96000), bytes(96000)], 'lux-id', user_labeled=True,
                               sample_ids=['i1','i2'], session_id='s1')
    assert stats['reference_count'] == 2
    restored = LocalSpeakers(None, 'test', bank.path, process.profiles)
    restored.embedding = lambda _: vector(.25)
    track, identified, value = restored.identify(np.ones(48000))
    assert identified and value == pytest.approx(math.cos(.15))
    assert restored.rank_known([])[0]['reference_count'] == 2
    assert {r['input_id'] for r in next(iter(restored.known.values()))['references']} == {'i1','i2'}


def test_legacy_multiple_entries_rank_as_one_person_without_rewriting_bank(process, tmp_path):
    path = tmp_path/'bank.json'
    text = json.dumps({'model_id':'test','entries':{
        'old1': {'object_id':'lux-id','embedding':vector(.1)},
        'old2': {'object_id':'lux-id','embedding':vector(.5)}}})
    path.write_text(text)
    bank = LocalSpeakers(None, 'test', path, process.profiles)
    bank.embedding = lambda _: vector(.3)
    assert len(bank.rank_known([])) == 1
    assert bank.rank_known([])[0]['reference_count'] == 2
    assert path.read_text() == text


def test_reference_limit_and_duplicate_do_not_give_quantity_bonus():
    rows = [{'embedding': vector(i*.15), 'input_id':str(i)} for i in range(20)]
    assert len(references({'references': rows})) == 10
    assert len(references({'references': [rows[0]]*10})) == 1


def test_candidates_reject_short_overlap_repeat_and_enforce_both_limits(tmp_path):
    pool = VoiceCandidates(tmp_path/'pool.db', max_actors=1, per_actor=2)
    assert pool.collect('short','a','s',[1.,0.],1) == 'ineligible'
    assert pool.collect('mixed','a','s',[1.,0.],3,overlap=True) == 'ineligible'
    assert pool.collect('i1','a','s',[1.,0.],3,start_ms=0,end_ms=3000) == 'collected'
    assert pool.collect('i1','a','s',[1.,0.],3) == 'duplicate'
    assert pool.collect('overlap','a','s',[1.,0.],3,start_ms=2000,end_ms=5000) == 'overlap'
    assert pool.collect('i2','a','s',[1.,0.],3,start_ms=3000,end_ms=6000) == 'collected'
    assert pool.collect('i3','a','s',[1.,0.],3) == 'sample_limit'
    assert pool.collect('b1','b','s',[1.,0.],3) == 'actor_limit'


def test_candidate_capacity_stops_growth_and_only_own_cache_expires(tmp_path):
    now = [0.]
    pool = VoiceCandidates(tmp_path/'pool.db', clock=lambda:now[0], max_bytes=0)
    assert pool.collect('i1','a','s',[1.,0.],3) == 'capacity_limit'
    pool.max_bytes = 1024*1024
    assert pool.collect('i1','a','s',[1.,0.],3) == 'collected'
    now[0] = 8*86400
    assert pool.collect('i2','a','s2',[1.,0.],3) == 'collected'
    with pool.connect() as db:
        assert db.execute('SELECT input_id FROM candidates').fetchall() == [('i2',)]


def test_known_candidate_needs_two_sessions_and_independent_semantics(process, tmp_path):
    bank = LocalSpeakers(None, 'test', tmp_path/'bank.json', process.profiles)
    bank._store_voice('lux-id', [1.,0.], b'', basis='manual_selection')
    for iid, session, angle in [('i1','s1',.2),('i2','s2',.35)]:
        bank.candidates.collect(iid,'lux-id',session,vector(angle),3)
    assert bank.review_candidate('i1','lux-id') == 'unverified'
    assert bank.review_candidate('i1','lux-id',agreement=True) == 'awaiting_independent_session'
    assert len(references(next(iter(bank.known.values())))) == 1
    assert bank.review_candidate('i2','lux-id',agreement=True) == 'promoted'
    assert len(references(next(iter(bank.known.values())))) == 3


def test_unknown_candidates_never_create_registered_voice(process, tmp_path):
    bank = LocalSpeakers(None, 'test', tmp_path/'bank.json', process.profiles)
    bank.candidates.collect('i1','unknown','s1',[1.,0.],3)
    bank.candidates.collect('i2','unknown','s2',[1.,0.],3)
    assert bank.review_candidate('i1','unknown',agreement=True) == 'not_registered'
    assert not bank.known


def test_failed_reference_save_keeps_old_bank_and_candidates_retryable(process, tmp_path, monkeypatch):
    bank = LocalSpeakers(None, 'test', tmp_path/'bank.json', process.profiles)
    bank._store_voice('lux-id',[1.,0.],b'',basis='manual_selection')
    old = json.dumps(bank.known)
    for iid, session, angle in [('i1','s1',.2),('i2','s2',.35)]:
        bank.candidates.collect(iid,'lux-id',session,vector(angle),3)
    bank.review_candidate('i1','lux-id',agreement=True)
    monkeypatch.setattr(bank,'_save_bank',lambda: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError):
        bank.review_candidate('i2','lux-id',agreement=True)
    assert json.dumps(bank.known) == old
    assert len(bank.candidates.verified('lux-id')) == 2


def test_feedback_is_played_targeted_time_bounded_and_does_not_invent_answer():
    now = [1.]
    feedback = InteractionFeedback(clock=lambda:now[0])
    assert feedback.snapshot()['questions'] == []
    feedback.played('r',0,'怎么称呼你？',['a'],['i0'])
    feedback.incoming('i1','b','张三',directed=True,received_at_ms=1100)
    assert feedback.snapshot(cutoff_ms=1100)['questions'][0]['state'] == 'awaiting_answer'
    feedback.incoming('i2','a','张三',directed=True,received_at_ms=1200)
    now[0] = 2.
    assert feedback.snapshot(cutoff_ms=1100)['questions'][0]['state'] == 'awaiting_answer'
    assert feedback.snapshot()['questions'][0]['state'] == 'possible_answer_received'
    feedback.main(['i2'],'继续核对称呼')
    assert feedback.snapshot()['questions'][0]['state'] == 'answer_candidate_processed'
    assert feedback.snapshot()['unfinished_notes'][0]['basis'] == '主流程短评，未对外说出'
    now[0] = 125.
    assert feedback.snapshot()['questions'] == []


def test_candidate_worker_does_not_block_entry_and_playback_feedback_reaches_jev(process, tmp_path):
    import asyncio
    from threading import Event
    from jshi.app.voice import VoiceConversation
    from jshi.voice.audio_buffer import SessionAudio
    from jshi.voice.volc import Transcript
    from jshi.voice.jev import VoiceJEV
    from tests.test_voice import FakeCloud
    from types import SimpleNamespace
    entered, release = Event(), Event()
    class Capture:
        request = None
        def generate(self, request):
            if request.purpose == 'voice_jev_scene':
                return SimpleNamespace(text='{"scene":"匠石已经询问怎样称呼，等待回答。"}')
            self.request = request
            return SimpleNamespace(text='{"items":[{"n":1,"to_jiangshi":"yes"}]}')
    async def run():
        bank = LocalSpeakers(None, 'test', tmp_path/'bank.json', process.profiles)
        bank.embedding = lambda _: [1.,0.]
        def slow_collect(*args, **kwargs):
            entered.set()
            release.wait(5)
            return 'collected'
        bank.collect_candidate = slow_collect
        audio = SessionAudio()
        audio.append(bytes(96000))
        model = Capture()
        async def send(_): pass
        conversation = VoiceConversation(process,'stone',FakeCloud(),send,local_speakers=bank,
                                         sample_source=audio,input_pause_s=0,jev=VoiceJEV(model))
        try:
            conversation.interaction_feedback.played('prior',0,'怎么称呼你？',['lux-id'],['old-input'])
            conversation.jev_scene_store.append('played:prior:0',{'p':'匠石','text':'怎么称呼你？','basis':'已播出','at':1})
            await conversation.accept(Transcript('匠石你好。','A',0,3000,True))
            await asyncio.wait_for(conversation.queue.join(),2)
            await asyncio.wait_for(conversation.turn_queue.join(),2)
            assert entered.is_set() and not release.is_set()
            data = json.loads(model.request.input_text)
            assert data['input']['unwritten'][0]['text'] == '怎么称呼你？'
            assert 'interaction_feedback' not in data
        finally:
            release.set()
            await conversation.close()
    asyncio.run(run())


def test_actual_player_ack_is_required_before_question_feedback(process):
    import asyncio
    from jshi.app.voice import VoiceConversation
    from tests.test_voice import FakeCloud
    async def run():
        async def send(_): pass
        conversation = VoiceConversation(process,'stone',FakeCloud(),send,input_pause_s=0)
        try:
            delivery = conversation.delivery.begin('怎么称呼你？','lux-id')
            assert conversation.interaction_feedback.snapshot()['questions'] == []
            await conversation.acknowledge({'reply_id':delivery.reply_id,'segment':0,'phase':'started'})
            assert conversation.interaction_feedback.snapshot()['questions'] == []
            await conversation.acknowledge({'reply_id':delivery.reply_id,'segment':0,'phase':'completed'})
            question = conversation.interaction_feedback.snapshot()['questions'][0]
            assert question['target_ids'] == ['lux-id'] and question['reply_id'] == delivery.reply_id
        finally:
            await conversation.close()
    asyncio.run(run())
