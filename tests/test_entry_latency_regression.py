import asyncio
import json
import sqlite3
from types import SimpleNamespace

from jshi.subject.domain import HistoryKind, HistoryRecord
from jshi.subject.repository import SubjectRepository
from jshi.voice.entry_scheduler import EntryScheduler
from jshi.voice.jev import BatchDecision, VoiceJEV
from jshi.voice.jev_material import build_snapshot
from jshi.voice.jev_serial import entry_route_conflicts, entry_schema, exact_identity_duplicates, unlisted_people_unknown
from jshi.voice.turn_schedule import TurnSchedule


def speech(i, text):
    return SimpleNamespace(input_id=i, text=text, start_ms=0, end_ms=1000, overlap=False,
        speaker=SimpleNamespace(object_id='person', track_id='track', status='recognized'))


def test_incomplete_background_stays_background_instead_of_waiting():
    # Actual 21:08:44 response: unfinished side conversation, explicitly not waiting.
    decision = BatchDecision(action='ignore', route='background', entry_route='background',
        interaction='none', control='keep', utterance_complete='incomplete',
        pending_refs=('I1',), reason='保留背景不等待')
    result = VoiceJEV.enforce_completeness(decision)
    assert result.route == 'background' and result.action == 'ignore'
    assert result.reason == decision.reason
    # Background context retention is separate from holding a response.
    assert result.pending_refs == ('I1',)
    normalized, _ = entry_route_conflicts({'interaction': 'none', 'route': 'wait'}, {'properties': {}})
    assert VoiceJEV.enforce_completeness(BatchDecision(action='ignore',
        route=normalized['route'], interaction=normalized['interaction'],
        utterance_complete='incomplete')).route == 'background'


def test_completeness_still_blocks_a_premature_answer_and_preserves_stop():
    decision = BatchDecision(action='respond', route='main', interaction='direct',
        utterance_complete='incomplete', direct_reply='抢答', waiting_reply='嗯')
    result = VoiceJEV.enforce_completeness(decision)
    assert result.route == 'wait' and result.action == 'ignore'
    assert not result.direct_reply and not result.waiting_reply
    stopped = VoiceJEV.enforce_completeness(BatchDecision(action='stop', control='stop',
        utterance_complete='incomplete', route='background', reason='明确要求停止'))
    assert stopped.action == 'stop' and stopped.control == 'stop' and stopped.route == 'background'


def test_unclear_relation_keeps_old_entry_instead_of_rejudging_merged_speech():
    schedule = TurnSchedule()
    schedule.open_matter()
    attempt = schedule.begin_attempt(('a',))
    schedule.note_text('b', '嗯')
    effect = schedule.apply_collision('uncertain', 'uncertain', schedule.current().pending, schedule.capture())
    assert effect.kind == 'reapprove' and attempt.execution == 'open'
    assert schedule.current().pending == []


def test_confident_continuation_still_replaces_old_entry():
    schedule = TurnSchedule()
    schedule.open_matter()
    attempt = schedule.begin_attempt(('a',))
    schedule.note_text('b', '还要加一个条件')
    effect = schedule.apply_collision('continue', 'hold', schedule.current().pending, schedule.capture())
    assert effect.kind == 'replace_entry' and attempt.execution == 'revoked'


def test_unclear_work_collision_keeps_cognition_and_does_not_pause():
    schedule = TurnSchedule()
    schedule.set_task('cognition', 'running')
    schedule.add_answer('pending', 1)
    schedule.note_text('b', '嗯')
    effect = schedule.apply_work_collision('uncertain', 'uncertain', schedule.capture())
    assert effect.kind == 'reapprove'
    assert schedule.current().answers['pending'].eligible
    supplement = TurnSchedule()
    supplement.set_task('cognition', 'running')
    supplement.add_answer('pending', 1)
    supplement.note_text('b', '还有，要便宜的')
    effect = supplement.apply_work_collision('continue', 'hold', supplement.capture())
    assert effect.kind == 'invalidate_work'
    assert not supplement.current().answers['pending'].eligible


def test_failed_collision_call_keeps_old_entry_result():
    async def run():
        class Service:
            config = {'mode': 'active', 'collision': True, 'collision_apply': True, 'threshold': .85}
            async def decide(self, purpose, state, revision):
                raise RuntimeError('provider down')
        scheduler = EntryScheduler(Service())
        release = asyncio.Event()
        async def operation():
            await release.wait()
            return 'old result'
        task = asyncio.create_task(scheduler.run([speech('a', '帮我查天气')], operation))
        await asyncio.sleep(0)
        scheduler.notify([speech('b', '嗯')])
        await asyncio.sleep(0.01)
        release.set()
        assert await task == 'old result'
        assert scheduler.effects[-1].kind == 'reapprove'
        await scheduler.close()
    asyncio.run(run())


def test_result_before_relation_waits_for_relation_instead_of_discarding():
    async def run():
        relation = asyncio.Event()
        class Service:
            config = {'mode': 'active', 'collision': True, 'collision_apply': True, 'threshold': .5}
            async def decide(self, purpose, state, revision):
                await relation.wait()
                return SimpleNamespace(status='success', answers={},
                    choice=lambda key, threshold: {'relation': 'independent', 'old_work': 'continue'}[key])
        scheduler = EntryScheduler(Service())
        async def operation():
            return 'old result'
        async def later():
            await asyncio.sleep(0.01)
            relation.set()
        task = asyncio.create_task(scheduler.run([speech('a', '帮我查天气')], operation))
        await asyncio.sleep(0)
        scheduler.notify([speech('b', '对了，你好')])
        asyncio.create_task(later())
        assert await task == 'old result'
        await scheduler.close()
    asyncio.run(run())


def test_shown_temporary_voice_code_is_selectable_but_not_confirmed():
    lines = [{'i': 1, 'p': 'P132', 'identity': 'unresolved', 'evidence_ref': 'I1',
              'candidates': [{'p': 'P132', 'source': 'temporary_voice'}, {'p': 'lux', 'source': 'voiceprint'}]}]
    people = entry_schema(lines, {'I1'})['properties']['items']['items']['properties']['person']['enum']
    assert {'P132', 'lux', 'unknown'} <= set(people)
    batch = [{'n': 1, 'who': 'P132', 'voice_evidence': {'pick': 'P132', 'strength': 'weak',
              'basis': '临时声音连续性；只认出同一个声音'},
              'candidates': [{'who': 'P132', 'source': 'temporary_voice'}, {'who': 'lux', 'source': 'voiceprint'}]}]
    decision = VoiceJEV()._from_current(
        [{'i': 1, 'person': 'P132', 'certainty': 'confirmed', 'why': '同一声音连续'}], batch, '')
    assert decision.items[0].speaker_pick == 'P132' and decision.items[0].speaker_level == '可能'


def test_unlisted_person_becomes_unknown_without_dropping_other_judgments():
    schema = entry_schema([{'i': 1, 'p': 'lux', 'evidence_ref': 'I1', 'candidates': []}], {'I1'})
    data = {'items': [{'i': 1, 'person': 'P999', 'certainty': 'tentative', 'why': '猜测'}], 'route': 'prepare'}
    fixed, changed = unlisted_people_unknown(data, schema)
    assert changed and fixed['items'][0]['person'] == 'unknown' and fixed['items'][0]['certainty'] == 'unknown'
    assert fixed['route'] == 'prepare'


def test_scene_candidates_listed_once_and_identical_evidence_referenced():
    scene = [{'who': f'P{n}', 'source': 'scene', 'names': [f'访客{n}']} for n in range(100, 110)]
    voice = {'who': 'lux', 'source': 'voiceprint', 'similarity': .2}
    batch = [{'n': n, 'candidates': [voice, *scene]} for n in (1, 2)]
    payload = {'input': {'lines': [{'i': n, 'evidence_ref': f'I{n}', 'voice': {'pick': '', 'strength': 'weak'}}
                                   for n in (1, 2)]}}
    snapshot = build_snapshot(payload, batch, {})
    shared = [e for e in snapshot['evidence'] if e.get('scope') == 'batch']
    assert len(shared) == 1 and len(shared[0]['candidates']) == 10
    rows = [e for e in snapshot['evidence'] if e.get('ref')]
    assert all(c['source'] != 'scene' for c in rows[0]['candidates'])
    assert rows[1] == {'ref': 'I2', 'same_as': 'I1'}
    text = json.dumps(snapshot['evidence'], ensure_ascii=False)
    assert text.count('访客105') == 1


def test_entry_normalization_settles_only_unambiguous_conflicts():
    schema = entry_schema([{'i': 1, 'p': 'lux', 'evidence_ref': 'I1', 'candidates': []}], {'I1'})
    data = {'type': 'json_object', 'interaction': 'none', 'route': 'background', 'process_refs': ['I1'], 'pending_refs': []}
    fixed, notes = entry_route_conflicts(data, schema)
    assert 'type' not in fixed and fixed['process_refs'] == []
    assert notes == ('response_format_echo', 'none_background_without_process')
    fixed, notes = entry_route_conflicts({'interaction': 'none', 'route': 'wait', 'pending_refs': ['I1']}, schema)
    assert fixed['route'] == 'background' and fixed['pending_refs'] == ['I1'] and notes == ('none_not_waiting',)
    direct = {'interaction': 'direct', 'route': 'background', 'process_refs': ['I1']}
    assert entry_route_conflicts(direct, schema) == (direct, ())

    rows = {'items': [{'i': 1, 'person': 'lux', 'certainty': 'tentative', 'why': '声纹弱'},
                      {'i': 1, 'person': 'lux', 'certainty': 'tentative', 'why': '另写一遍'}]}
    fixed, changed = exact_identity_duplicates(rows)
    assert changed and fixed['items'] == rows['items'][:1]
    differing = {'items': [rows['items'][0], {**rows['items'][1], 'certainty': 'confirmed'}]}
    assert exact_identity_duplicates(differing) == (differing, False)


def test_voice_conclusion_keeps_threshold_for_comparison():
    voice = {'pick': 'lux', 'strength': 'clear', 'basis': '本句声纹比较；相似度不是概率',
             'score': .46086, 'gap': .1234, 'threshold': .33, 'margin': .08, 'debug': 'x'}
    payload = {'input': {'lines': [{'i': 1, 'evidence_ref': 'I1', 'voice': voice}]}}
    snapshot = build_snapshot(payload, [{'n': 1, 'candidates': []}], {})
    row = next(e for e in snapshot['evidence'] if e.get('ref') == 'I1')
    assert row['voice'] == {'pick': 'lux', 'strength': 'clear', 'basis': '本句声纹比较；相似度不是概率',
                            'score': .461, 'gap': .123, 'threshold': .33, 'margin': .08}


def test_history_is_queued_in_order_and_visible_to_readers(tmp_path):
    repository = SubjectRepository(tmp_path / 'subject.db')
    for n in range(50):
        repository.add_history(HistoryRecord(subject_id='s', kind=HistoryKind.FACT,
                                             event_type='diagnostic', content={'n': n}, source_ids=()))
    rows = repository.list_history('s', HistoryKind.FACT, limit=100)
    assert [r.content['n'] for r in rows] == list(range(50))
    with sqlite3.connect(tmp_path / 'subject.db') as connection:
        assert connection.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'


def test_work_invalidation_caused_by_this_batch_does_not_discard_it():
    from jshi.app.voice import VoiceConversation
    conversation = VoiceConversation.__new__(VoiceConversation)
    conversation.epoch = 3
    conversation.work_epoch_inputs = {}
    conversation._bump_work_epoch(SimpleNamespace(covered=('b',)))
    assert conversation._epoch_current(3, {'b'})
    assert not conversation._epoch_current(3, {'c'})
    conversation._bump_work_epoch(SimpleNamespace(covered=()))
    assert not conversation._epoch_current(3, {'b'})
