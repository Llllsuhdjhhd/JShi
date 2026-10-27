"""Evidence flow contracts: scope, hard budgets, visibility, corrections, and feedback."""
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jshi.assembly import AssemblyContext, AssemblyFragment, CurrentStateAssembler, MemorySource
from jshi.assembly.port import LoadResult
from jshi.core import params
from jshi.memory.contracts import RecallRequestContext
from jshi.memory.port import RecalledFragment
from jshi.memory.recall_flow import RecallFlow
from jshi.memory.selection import select_candidates
from jshi.memory.shell import MemoryShell
from jshi.memory.working_set import RecallWorkingSetStore
from jshi.reflection.memory_store import ReflectionMemoryStore
from jshi.models import ModelResponse, MemoryRating, MemoryRatings


def event(ident, content='QF就是小秦，也是勤奋。', **kwargs):
    return RecalledFragment(ident, 'event', content, content=content, object_id='person', **kwargs)


class Backend:
    def __init__(self):
        self.items = (event('old'),)
        self.calls = []
        self.error = False

    def recall(self, subject, query, **kwargs):
        self.calls.append((query, kwargs))
        if self.error:
            raise RuntimeError('unavailable')
        return self.items[:kwargs.get('limit', 12)]


@pytest.fixture
def flow(tmp_path):
    store = ReflectionMemoryStore(tmp_path / 'reflection.sqlite3')
    backend = Backend()
    memory = MemoryShell(backend)
    memory.reflection_store = store
    working = RecallWorkingSetStore(store)
    return RecallFlow(memory, store, working), backend, store


def request(**kwargs):
    return RecallRequestContext('stone', 'activity', 'session', 'QF和小秦是什么关系？',
                                object_ids=('person',), interlocutor_object_id='person',
                                created_at=time.time(), **kwargs)


def test_complete_budget_skips_long_but_keeps_later_short():
    source = SimpleNamespace(name='memory', status='implemented', load=lambda ctx: LoadResult(fragments=(
        AssemblyFragment('memory', 'memory:long', 'x' * 101, 'fact'),
        AssemblyFragment('memory', 'memory:short', 'not the wife', 'fact'))))
    result = CurrentStateAssembler((source,)).assemble(AssemblyContext('stone', 'q', working_set_limit=15))
    assert [f.id for f in result.fragments] == ['memory:short']
    assert result.report[0].skipped_reasons == (('memory:long', 'budget'),)


def test_activity_excerpt_no_longer_bypasses_memory_budget():
    source = SimpleNamespace(name='activity', status='implemented', load=lambda ctx: LoadResult(fragments=(
        AssemblyFragment('activity', 'scene', 'scene' * 100, 'context_view'),
        AssemblyFragment('activity', 'memory:old', 'old' * 100, 'recall_excerpt'))))
    result = CurrentStateAssembler((source,)).assemble(AssemblyContext('stone', 'q', working_set_limit=10))
    assert [f.id for f in result.fragments] == ['scene']


def test_selection_does_not_strip_negation_or_invent_corrections():
    selected, _ = select_candidates((event('old', 'QF是小琴'), event('new', 'QF不是小琴')),
                                     max_items=5, max_chars=100)
    assert len(selected) == 2
    selected, skipped = select_candidates((event('old'), event('new', supersedes=('old',))),
                                           max_items=5, max_chars=100)
    assert [f.event_id for f in selected] == ['new']
    assert ('old', 'superseded') in skipped


def test_zero_results_exact_link_and_unknown_effect(flow):
    runner, backend, store = flow
    backend.items = ()
    batch = runner.load_candidates(request())
    runner.record_visible(batch, ())
    assert store.pending('stone') is None  # Wait for this exact main call.
    runner.complete(batch, (), ModelResponse(text='不确定', model='test'), {})
    row = store.pending('stone')
    assert row['status'] == 'empty'
    assert row['run_id'] == batch.request.run_id
    assert json.loads(row['payload']) == []
    assert json.loads(row['outcome'])['response_status'] == 'planned'


def test_working_set_reuses_same_topic_and_isolates_actor_session(flow):
    runner, backend, _ = flow
    batch = runner.load_candidates(request())
    runner.record_visible(batch, batch.candidates)
    runner.complete(batch, batch.candidates, ModelResponse(text='同一个人', model='test'), {'M1': 'old'})
    backend.items = ()
    following = runner.load_candidates(request())
    assert following.reused_ids == ('old',)
    assert not runner.load_candidates(replace(request(), object_ids=('other',))).candidates
    assert not runner.load_candidates(replace(request(), session_id='other')).candidates
    assert not runner.load_candidates(replace(request(), query_text='明天台风天气')).candidates
    assert runner.load_candidates(replace(request(), query_text='继续')).candidates


def test_stale_commit_cannot_overwrite_correction(flow):
    runner, _, _ = flow
    stale = runner.load_candidates(request())
    first = runner.load_candidates(request())
    runner.working_set.commit_visible(first.request, [event('new')], expected_version=first.base_version)
    assert not runner.working_set.commit_visible(stale.request, [event('old')], expected_version=stale.base_version)
    _, restored = runner.working_set.load_scope(request(), ttl=1800, similarity=.2)
    assert [f.event_id for f in restored] == ['new']


def test_invalid_reference_ignored_and_misleading_not_retained(flow):
    runner, _, store = flow
    batch = runner.load_candidates(request())
    runner.record_visible(batch, batch.candidates)
    response = ModelResponse(text='纠正', model='test', memory_ratings=MemoryRatings(items=(
        MemoryRating('M1', misleading=True), MemoryRating('M99', relevance='related'))))
    normalized = runner.complete(batch, batch.candidates, response, {'M1': 'old'})
    assert [item.ref for item in normalized.items] == ['old']
    assert not runner.working_set.load_scope(request(), ttl=1800, similarity=.2)[1]
    outcome = json.loads(store.pending('stone')['outcome'])
    assert outcome['main_ratings']['items'][0]['ref'] == 'old'


def test_candidate_expansion_is_not_multiplied_twice(flow):
    runner, backend, store = flow
    store.observe('stone', request().query_text, 'person', [event('old')])
    observation = store.pending('stone')
    store.save_feedback(observation, [{'event_id': 'old', 'judgment': 'helpful', 'reason': 'alias'}], {'old'})
    runner.load_candidates(request(candidate_limit=9))
    assert backend.calls[-1][1]['limit'] == 9


def test_disabled_does_not_retrieve_or_reuse(flow):
    runner, backend, store = flow
    batch = runner.load_candidates(request(enabled=False))
    assert batch.status == 'disabled' and not backend.calls
    runner.record_visible(batch, ())
    runner.complete(batch, (), ModelResponse(text='hello', model='test'), {})
    assert store.pending('stone') is None


def test_query_error_preserves_reuse_and_failed_gap_is_distinct(flow):
    runner, backend, _ = flow
    first = runner.load_candidates(request())
    runner.working_set.commit_visible(first.request, first.candidates, expected_version=0)
    backend.error = True
    following = runner.load_candidates(request(), ('missing old promise',))
    assert following.status == 'error'
    assert following.candidates and all(q['status'] == 'error' for q in following.query_runs)


def test_gap_query_is_not_restricted_to_current_speaker(flow):
    runner, backend, _ = flow
    runner.load_candidates(request(), ('lux住在云南',))
    assert backend.calls[-1][1]['object_ids'] == ()
    assert backend.calls[-1][1]['interlocutor_object_id'] is None


def test_working_set_survives_store_reopen(flow):
    runner, _, store = flow
    batch = runner.load_candidates(request())
    runner.working_set.commit_visible(batch.request, batch.candidates, expected_version=0)
    reopened = RecallWorkingSetStore(ReflectionMemoryStore(store.path))
    assert reopened.load_scope(request(), ttl=1800, similarity=.2)[1][0].event_id == 'old'


def test_main_path_records_actual_visibility_and_clean_query(tmp_path):
    from jshi.identity import IdentityProfile, IdentityRepository
    from jshi.subject import SubjectProcess, SubjectRepository
    from jshi.recognition import ObjectProfile
    identities = IdentityRepository(tmp_path / 'identities.json')
    identities.create(IdentityProfile('stone', '匠石', 'test', '我是匠石'))
    backend = Backend()
    process = SubjectProcess(SubjectRepository(tmp_path / 'subject.sqlite3'), identities,
                            SimpleNamespace(name='model', generate=lambda req: ModelResponse(text='同一个人', model='model')),
                            memory=MemoryShell(backend))
    process.profiles.create(ObjectProfile(object_id='person', label='user', source='test', status='confirmed'))
    result = process.experience('stone', 'QF和小秦是什么关系？\nJEV 初判：人物归属见标注', object_ref='user')
    assert backend.calls[0][0] == 'QF和小秦是什么关系？'
    row = process.reflection_memory.pending('stone')
    assert row['activity_id'] == result.activity.id
    assert json.loads(row['outcome'])['model_visible_ids'] == ['old']
    assert result.current_state.recall_batch.request.activity_id == result.activity.id


def test_ratings_projection_is_one_sample_per_activity(tmp_path):
    from jshi.effectiveness.ratings import JsonlRatingStore, RatingRow
    path = tmp_path / 'ratings.jsonl'
    store = JsonlRatingStore(path)
    store.append(RatingRow('stone', 'activity', coverage='thin'))
    store.append(RatingRow('stone', 'activity', coverage='sufficient'))
    assert len(JsonlRatingStore(path).list()) == 1
    assert store.list()[0].coverage == 'sufficient'


def test_evaluator_uses_short_refs_and_does_not_always_reprocess(flow):
    from jshi.reflection.executor import ReflectionExecutor
    runner, _, store = flow
    batch = runner.load_candidates(request())
    runner.record_visible(batch, batch.candidates)
    runner.complete(batch, batch.candidates, ModelResponse(text='同一个人', model='test'), {'M1': 'old'})
    executor = object.__new__(ReflectionExecutor)
    executor.store = store
    executor.process = SimpleNamespace(activity_ledger=SimpleNamespace(list_experiences=lambda subject: ()))
    captured = []
    def ask(mode, subject, payload):
        captured.append(payload)
        return {'feedback': [{'event_id': 'E1', 'judgment': 'helpful', 'reason': '别名证据'},
                             {'event_id': 'ghost', 'judgment': 'helpful', 'reason': '不可评分'}],
                'coverage': 'sufficient', 'needs_reprocess': False}
    executor.ask, executor.audit = ask, lambda *args: None
    executor.evaluate('stone', store.pending('stone'))
    assert captured[0]['events'][0]['event_id'] == 'E1'
    assert captured[0]['main_cognition']['visible'] == ['E1']
    assert 'run_id' not in json.dumps(captured[0]) and 'event_id": "old"' not in json.dumps(captured[0])
    assert store.pending('stone') is None
    with store.connect() as db:
        saved = db.execute('SELECT evaluation FROM observations WHERE run_id=?', (batch.request.run_id,)).fetchone()[0]
    assert json.loads(saved)['reviewed_ids'] == ['old']
    assert len(json.loads(saved)['feedback']) == 1


def test_empty_evaluation_can_report_gap_without_inventing_event(flow):
    from jshi.reflection.executor import ReflectionExecutor
    runner, backend, store = flow
    backend.items = ()
    batch = runner.load_candidates(request())
    runner.record_visible(batch, ())
    runner.complete(batch, (), ModelResponse(text='不确定', model='test'), {})
    executor = object.__new__(ReflectionExecutor)
    executor.store = store
    executor.process = SimpleNamespace(activity_ledger=SimpleNamespace(list_experiences=lambda subject: ()))
    executor.ask = lambda *args: {'feedback': [], 'coverage': 'missing', 'gap_query': 'QF别名'}
    executor.audit = lambda *args: None
    executor.evaluate('stone', store.pending('stone'))
    with store.connect() as db:
        assert json.loads(db.execute('SELECT evaluation FROM observations').fetchone()[0])['coverage'] == 'missing'


def test_overlapping_runs_only_receive_their_own_outcome(flow):
    runner, _, store = flow
    first = runner.load_candidates(request())
    second = runner.load_candidates(replace(request(), activity_id='other-activity'))
    runner.record_visible(first, first.candidates)
    runner.record_visible(second, second.candidates)
    runner.complete(second, second.candidates, ModelResponse(text='second', model='test'), {'M1': 'old'})
    row = store.pending('stone')
    assert row['run_id'] == second.request.run_id
    with store.connect() as db:
        original = json.loads(db.execute('SELECT outcome FROM observations WHERE run_id=?', (first.request.run_id,)).fetchone()[0])
    assert original['response_status'] == 'not_generated'


def test_feedback_scope_includes_all_people(flow):
    runner, _, store = flow
    batch = runner.load_candidates(replace(request(), object_ids=('person', 'other')))
    runner.record_visible(batch, batch.candidates)
    runner.complete(batch, batch.candidates, ModelResponse(text='x', model='test'), {'M1': 'old'})
    row = store.pending('stone')
    store.save_feedback(row, [{'event_id': 'old', 'judgment': 'helpful', 'reason': 'two people'}], {'old'})
    assert not store.adjustments('stone', request().query_text, 'person', object_ids=('person',))
    assert store.adjustments('stone', request().query_text, 'person', object_ids=('person', 'other'))


def test_model_failure_does_not_commit_working_set(flow):
    runner, _, store = flow
    batch = runner.load_candidates(request())
    runner.record_visible(batch, batch.candidates)
    store.fail_run(batch.request.run_id, 'TurnCancelled')
    assert not runner.working_set.load_scope(request(), ttl=1800, similarity=.2)[1]
    assert json.loads(store.pending('stone')['outcome'])['response_status'] == 'TurnCancelled'


def test_web_data_exposes_scoped_recall_records(flow):
    from jshi.app.web_data import WebData
    runner, _, store = flow
    batch = runner.load_candidates(request())
    runner.record_visible(batch, batch.candidates)
    runner.complete(batch, batch.candidates, ModelResponse(text='x', model='test'), {})
    # The fixture filename is arbitrary; the web interface uses the production name.
    target = store.path.parent / 'reflection_memory.sqlite3'
    with store.connect() as db:
        import sqlite3
        with sqlite3.connect(target) as destination:
            db.backup(destination)
    assert WebData(store.path.parent, 'other').read('reflection:observations')['total'] == 0
    assert WebData(store.path.parent, 'stone').read('reflection:observations')['total'] == 1


def test_unknown_helpfulness_does_not_become_zero_score():
    from jshi.effectiveness.examples import features_from_ratings, features_from_rows
    from jshi.effectiveness.ratings import row_from_ratings
    value = MemoryRatings(items=(MemoryRating('event', relevance='related'),))
    features = features_from_ratings(value)
    assert features.helps_rated_count == 0
    assert features.precision_failure == 0
    assert features_from_rows([row_from_ratings('stone', 'activity', value)]) == features
    explicit_zero = features_from_ratings(MemoryRatings(items=(
        MemoryRating('event', relevance='related', helps_understanding=0),)))
    assert explicit_zero.helps_rated_count == 1 and explicit_zero.precision_failure > 0


def test_unlabeled_and_legacy_features_cannot_calibrate_new_quality_scale():
    from jshi.effectiveness.baseline import calibrate
    from jshi.effectiveness.examples import RatingExample
    assert calibrate([RatingExample(quality_label='unlabeled')]) is None
    assert calibrate([RatingExample(schema_version='ratings-example-v1')]) is None
    assert calibrate([RatingExample(quality_label='poor')]).poor_count == 1


def test_continue_after_unrelated_turn_does_not_reopen_old_topic(flow):
    runner, backend, _ = flow
    first = runner.load_candidates(request())
    runner.working_set.commit_visible(first.request, first.candidates, expected_version=first.base_version)
    backend.items = ()
    new_topic = runner.load_candidates(replace(request(), activity_id='weather', query_text='明天台风天气'))
    runner.record_visible(new_topic, ())
    runner.complete(new_topic, (), ModelResponse(text='天气', model='test'), {})
    assert not runner.load_candidates(replace(request(), query_text='继续')).candidates
    assert runner.load_candidates(request()).candidates  # Explicitly returning to the old question is allowed.


def test_new_run_reservation_blocks_old_commit_before_new_model_finishes(flow):
    runner, _, _ = flow
    old = runner.load_candidates(request())
    new = runner.load_candidates(replace(request(), activity_id='new'))
    assert not runner.working_set.commit_visible(old.request, old.candidates, expected_version=old.base_version)
    assert runner.working_set.commit_visible(new.request, new.candidates, expected_version=new.base_version)


def test_explicit_correction_evicts_cached_old_event(flow):
    runner, backend, _ = flow
    first = runner.load_candidates(request())
    runner.working_set.commit_visible(first.request, first.candidates, expected_version=0)
    backend.items = (event('new', 'QF不是小琴', supersedes=('old',)),)
    correction = runner.load_candidates(replace(request(), activity_id='corrected'))
    assert [item.event_id for item in correction.candidates] == ['new']
    runner.record_visible(correction, correction.candidates)
    runner.complete(correction, correction.candidates, ModelResponse(text='已纠正', model='test'), {})
    backend.items = ()
    assert [item.event_id for item in runner.load_candidates(request()).candidates] == ['new']


def test_old_excerpt_becomes_labeled_candidate_without_inventing_actor(flow):
    runner, backend, _ = flow
    backend.items = ()
    batch = runner.load_candidates(request(), legacy_excerpts=(('memory:legacy', 'QF和小秦同一个人'),))
    assert batch.candidates[0].object_id is None
    assert '归属及版本待核实' in batch.candidates[0].content


def test_shadow_does_not_commit_or_feed_back(flow):
    runner, _, store = flow
    batch = runner.load_candidates(replace(request(), mode=1))
    runner.record_visible(batch, batch.candidates)
    runner.complete(batch, batch.candidates, ModelResponse(text='x', model='test'), {})
    assert not runner.working_set.load_scope(request(), ttl=1800, similarity=.2)[1]
    assert store.pending('stone') is None


def test_working_set_separates_current_speaker_with_same_participants(flow):
    runner, backend, _ = flow
    alice = replace(request(), object_ids=('alice', 'bob'), interlocutor_object_id='alice')
    backend.items = (event('alice-work', 'Alice is a teacher', query_object_role='interlocutor'),)
    first = runner.load_candidates(alice)
    runner.working_set.commit_visible(alice, first.candidates, expected_version=first.base_version)
    backend.items = ()
    bob = replace(alice, activity_id='bob-turn', run_id='bob-run', interlocutor_object_id='bob')
    assert not runner.load_candidates(bob).candidates
    same_speaker = replace(alice, activity_id='alice-again', run_id='alice-again', object_ids=('bob', 'alice'))
    assert [x.event_id for x in runner.load_candidates(same_speaker).candidates] == ['alice-work']


@pytest.mark.parametrize('reason', ['characters', 'count', 'superseded_status'])
def test_invalidation_survives_candidate_filtering(flow, reason):
    runner, backend, store = flow
    first = runner.load_candidates(request())
    runner.working_set.commit_visible(first.request, first.candidates, expected_version=0)
    if reason == 'characters':
        backend.items = (event('correction', 'x' * 25001, supersedes=('old',)),)
    elif reason == 'count':
        backend.items = (event('unrelated'), event('correction', supersedes=('old',)))
    else:
        backend.items = (event('old', validity='superseded'),)
    req = replace(request(), activity_id='correction', candidate_limit=1 if reason == 'count' else 12)
    # Return the already fetched pool so the count branch tests the flow's final cap.
    runner.memory = SimpleNamespace(recall=lambda *a, **k: backend.items)
    batch = runner.load_candidates(req)
    assert 'old' in batch.invalidated_ids
    assert all(x.event_id not in {'old', 'correction'} for x in batch.candidates)
    runner.record_visible(batch, batch.candidates)
    runner.complete(batch, batch.candidates, ModelResponse(text='ok', model='test'), {})
    with store.connect() as db:
        details = json.loads(db.execute('SELECT details FROM observations WHERE run_id=?', (req.run_id,)).fetchone()[0])
    assert details['invalidated_ids'] == ['old']
    backend.items = ()
    assert 'old' not in {x.event_id for x in runner.load_candidates(request()).candidates}


@pytest.mark.parametrize('mode,enabled', [(1, True), (2, False)])
@pytest.mark.parametrize('transition', ['failure', 'timeout', 'evaluate', 'reprocess', 'compare'])
def test_non_evaluating_runs_never_enter_background_queue(flow, mode, enabled, transition):
    runner, _, store = flow
    req = replace(request(), mode=mode, enabled=enabled, created_at=time.time() - 600)
    batch = runner.load_candidates(req)
    runner.record_visible(batch, batch.candidates)
    if transition == 'failure':
        store.fail_run(req.run_id, 'TurnCancelled')
    elif transition != 'timeout':
        with store.connect() as db:
            db.execute('UPDATE observations SET stage=? WHERE run_id=?', (transition, req.run_id))
    assert store.pending('stone') is None
    with store.connect() as db:
        assert db.execute('SELECT stage FROM observations WHERE run_id=?', (req.run_id,)).fetchone()[0] == 'done'
        assert db.execute('SELECT count(*) FROM feedback').fetchone()[0] == 0


@pytest.mark.parametrize('transition', ['failure', 'timeout'])
def test_active_runs_still_evaluate_after_failure_or_timeout(flow, transition):
    runner, _, store = flow
    batch = runner.load_candidates(replace(request(), created_at=time.time() - 600))
    runner.record_visible(batch, batch.candidates)
    if transition == 'failure':
        store.fail_run(batch.request.run_id, 'TurnCancelled')
    assert store.pending('stone')['stage'] == 'evaluate'


def test_migration_preserves_legacy_observations_and_is_idempotent(tmp_path):
    import sqlite3
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE observations(id INTEGER PRIMARY KEY,subject TEXT,query TEXT,object_id TEXT,payload TEXT,created REAL,stage TEXT DEFAULT 'evaluate')")
        db.execute("INSERT INTO observations(subject,query,object_id,payload,created) VALUES('stone','old','person','[]',?)", (time.time(),))
    first = ReflectionMemoryStore(path)
    reopened = ReflectionMemoryStore(path)
    assert reopened.pending('stone')['query'] == 'old'
    assert first.pending('stone')['run_id'] == ''


def test_multi_speaker_query_keeps_attribution_and_known_scope(tmp_path):
    from tests.test_assembly import runtime
    from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence, SceneUtterance
    from jshi.recognition import ObjectProfile
    process, _, _ = runtime(tmp_path)
    backend = Backend()
    backend.items = ()
    process.recall_flow.memory = MemoryShell(backend)
    process.profiles.create(ObjectProfile(object_id='qf', label='勤奋', source='test', status='confirmed'))
    first = SpeakerEvidence('a', object_id='OBJ-USER', label='user', status='recognized', method='voiceprint_match')
    second = SpeakerEvidence('b', object_id='qf', label='勤奋', status='recognized', method='voiceprint_match')
    envelope = InputEnvelope('session', 'voice', (InputPart('text', '程序入口材料'),), first,
        current_utterances=(SceneUtterance('我们是什么关系？', first, 0, 10, input_id='one'),
                            SceneUtterance('你说说看。', second, 10, 20, input_id='two')))
    process.experience('stone', envelope.text, object_ref='user', envelope=envelope)
    assert backend.calls[0][0] == 'user：我们是什么关系？\n勤奋：你说说看。'
    assert set(backend.calls[0][1]['object_ids']) == {'OBJ-USER', 'qf'}


def test_literal_question_about_jev_is_not_removed_from_query(tmp_path):
    from tests.test_assembly import runtime
    process, _, _ = runtime(tmp_path)
    backend = Backend()
    backend.items = ()
    process.recall_flow.memory = MemoryShell(backend)
    process.experience('stone', 'JEV 初判是什么意思？', object_ref='user')
    assert backend.calls[0][0] == 'JEV 初判是什么意思？'


def test_malformed_optional_evaluation_fields_cannot_poison_next_query():
    from jshi.reflection.thoughts import RecallEvaluationThought
    value = RecallEvaluationThought(None).parse({'feedback': [{'event_id': 'E1', 'judgment': [], 'reason': 'x'}],
        'coverage': {}, 'gap_query': {}, 'needs_reprocess': 'true', 'reprocess_reason': []})
    assert value['feedback'] == [] and value['coverage'] == 'unknown'
    assert value['gap_query'] == '' and not value['needs_reprocess']


def test_generic_main_renders_conflict_as_evidence_status(tmp_path):
    from tests.test_assembly import runtime
    process, _, _ = runtime(tmp_path)
    context = process._model_context('stone', (), (
        AssemblyFragment('memory', 'memory:event', '原判断与新陈述不一致', 'fact', status='conflict'),))
    assert next(item for item in context if item['id'] == 'M1')['content'].startswith('（存在冲突）')
