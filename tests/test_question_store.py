import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from jshi.core.param_overlay import ParamSnapshot
from jshi.reflection.memory_store import ReflectionMemoryStore
from jshi.reflection.port import IntrospectionRequest, IntrospectionTrigger


def question(subject='a', versions=('activity:1.v1',), **changes):
    request = IntrospectionRequest(
        subject, IntrospectionTrigger.EVENT_RESULT, 'activity:1', datetime(2026, 1, 2, tzinfo=timezone.utc),
        entry_text='Which condition was missing?', dedup_key='missing-condition:1',
        evidence={'source_versions': versions}, object_id='p',
    )
    return replace(request, **changes)


def test_scan_scope_idempotence_and_restart(tmp_path):
    path = tmp_path / 'reflection.sqlite'
    store = ReflectionMemoryStore(path)
    q = question()
    kwargs = dict(cursor_key='ledger', cursor=8, expected_cursor=0,
                  coverage={'listed':[3, 4, 8], 'examined':[3, 4]})
    assert store.questions.submit('a', [q, q], shadow=True, **kwargs)
    assert not store.questions.submit('a', [q], shadow=True, **kwargs)
    assert not store.questions.has_pending('a')
    assert store.questions.cursor('a', 'ledger') == 0
    assert store.questions.submit('a', [q], **kwargs)
    assert store.questions.submit('a', [replace(q, request_id='duplicate-delivery')])
    with pytest.raises(ValueError):
        store.questions.submit('b', [q], cursor_key='ledger', cursor=8)
    other = ReflectionMemoryStore(path)
    assert other.questions.cursor('a', 'ledger') == 8
    assert other.questions.cursor('b', 'ledger') == 0
    assert other.pending_thought('a') is None
    with other.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM inner_shadow_candidates').fetchone()[0] == 1
        row = db.execute('SELECT coverage FROM inner_scans WHERE shadow=0').fetchone()
        assert json.loads(row[0]) == kwargs['coverage']
    job = other.questions.claim('a', request_id='duplicate-delivery')
    assert job['revision'] == 1 and job['request'].entry_at == q.entry_at
    assert not other.questions.has_pending('b')


def test_lease_freezes_once_and_stale_owner_cannot_commit(tmp_path):
    path = tmp_path / 'r.sqlite'
    stores = [ReflectionMemoryStore(path).questions for _ in range(2)]
    stores[0].submit('a', [question()])
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda s: s.claim('a', now=1, lease_seconds=5), stores))
    first, = [job for job in claims if job]
    assert stores[0].freeze_input(first, {'visible':['E1']}, now=2)
    assert not stores[0].freeze_input(first, {'visible':['E9']}, now=3)
    assert stores[1].claim('a', now=4) is None
    second = stores[1].claim('a', now=8)
    assert second['job_key'] == first['job_key'] and second['input'] == {'visible':['E1']}
    assert not stores[0].freeze(first, {'answer':'stale'}, now=8)
    assert not stores[0].release(first)
    assert stores[1].freeze(second, {'answer':'saved'}, now=8)
    assert stores[1].release(second)
    third = stores[0].claim('a', now=10)
    assert third['frozen'] == {'answer':'saved'}
    assert stores[0].complete(third, now=10)
    assert not stores[0].complete(third, now=10)
    assert not stores[0].has_pending('a')


def test_revision_waits_for_new_facts_and_replay_is_inert(tmp_path):
    s = ReflectionMemoryStore(tmp_path / 'q.sqlite').questions
    original = question(versions=['A','B'])
    s.submit('a',[original])
    old = s.claim('a')
    s.submit('a',[question(versions=['B','A'])])
    assert not s.has_pending('a')
    s.submit('a',[question(versions=['A','C'])])
    assert not s.freeze(old, {'stale':True})
    latest = s.claim('a')
    assert latest['revision'] == 2
    assert s.freeze(latest, {'question':'waiting for a source'})
    assert s.complete(latest, state='waiting')
    s.submit('a',[question(versions=['A']), question(versions=['A','B'])])
    assert not s.has_pending('a')
    s.submit('a',[question(versions=['D']), question(versions=['D'])])
    assert s.claim('a')['revision'] == 3


def test_followup_inherits_original_evidence_and_lookup_is_scoped(tmp_path):
    s = ReflectionMemoryStore(tmp_path/'r.sqlite').questions
    original = question(evidence={'source_versions':['condition'], 'segment_ids':['s1'], 'tool_facts':[{'code':'A'}]})
    s.submit('a', [original])
    update = question(evidence={'source_versions':['late-result'], 'segment_ids':['s2'], 'tool_facts':[{'code':'B'}]})
    s.submit('a', [update, update])
    reopened = s.lookup('a', original.request_id)
    assert reopened.request_id == update.request_id
    assert reopened.evidence['segment_ids'] == ['s1','s2']
    assert reopened.evidence['tool_facts'] == [{'code':'A'},{'code':'B'}]
    assert s.lookup('b', original.request_id) is None
    job = s.claim('a')
    assert job['revision'] == 2 and job['request'] == reopened


def test_full_queue_retains_requested_work_and_admits_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr('jshi.reflection.question_store.introspection_max_queue', lambda:1)
    store = ReflectionMemoryStore(tmp_path/'r.sqlite')
    s = store.questions
    q1, q2 = question(dedup_key='first'), question(dedup_key='second')
    s.submit('a',[q1, q2])
    with store.connect() as db:
        assert [row[0] for row in db.execute('SELECT state FROM inner_runs ORDER BY job_id')] == ['pending','backlog']
    first = s.claim('a')
    assert not s.claim('a')
    s.complete(first)
    s.submit('a',[question(dedup_key='third')])
    assert s.claim('a')['request'].request_id == q2.request_id


def test_persisted_signals_and_empty_discovery_are_atomic(tmp_path):
    s = ReflectionMemoryStore(tmp_path / 'q.sqlite').questions
    q = question()
    s.put_signals('a', [q,q]); s.put_signals('a', [q], shadow=True)
    assert s.has_signals('a')
    batch, last = s.next_signals('a')
    assert [r.request_id for r in batch] == [q.request_id]
    assert s.submit('a', [], cursor_key='signals',cursor=last,expected_cursor=0,coverage=[q.entry_ref])
    assert not s.has_signals('a') and s.has_signals('a', shadow=True)
    assert not s.has_pending('a')
    assert not s.submit('a', [q], cursor_key='signals',cursor=last,expected_cursor=0)
    assert not s.has_pending('a')


def test_synchronous_manual_requests_and_post_commit_review(tmp_path, monkeypatch):
    store = ReflectionMemoryStore(tmp_path / 'q.sqlite')
    s = store.questions
    manual = question(trigger=IntrospectionTrigger.MANUAL)
    job = s.submit_and_claim(manual)
    assert job and s.claim('a') is None
    second_manual = s.submit_and_claim(replace(manual, request_id='another-explicit-command'))
    assert s.complete(second_manual)
    results = [{'id':'sample'}]
    assert s.freeze(job, {'proposals':results})
    monkeypatch.setattr('jshi.core.params.introspection_max_queue', lambda:1)
    existing = store.enqueue_thought('a','older',{},'older')
    assert not s.complete(job, proposals=results, activity_id='stable-act')
    store.finish_thought(existing)
    retry = s.claim('a')
    assert retry['job_key'] == job['job_key'] and retry['frozen']['proposals'] == results
    assert s.complete(retry, proposals=results, activity_id='stable-act')
    assert not s.complete(job, proposals=results, activity_id='stable-act')
    pending = store.pending_thought('a')
    assert pending['mode'] == 'review_behavior_result'
    assert json.loads(pending['payload']) == {'proposals':results}


def test_atomic_recall_handoff_routes_and_recovers_failure(tmp_path, monkeypatch):
    store = ReflectionMemoryStore(tmp_path / 'q.sqlite')
    request = SimpleNamespace(subject_id='a',activity_id='activity:1',input_revision=1,run_id='run',
                              query_text='original question',interlocutor_object_id=None,created_at=123.,
                              object_ids=(),mode=2,session_id='s',policy_version=1)
    batch = SimpleNamespace(request=request,candidates=(),query_runs=(),skipped=(),invalidated_ids=(),
                            reused_ids=(),elapsed_ms=1,truncated=False,status='ready')
    monkeypatch.setattr(store, 'discovery_mode', lambda db=None:2)
    store.observe_run(batch, [])
    store.link_run_outcome('run', {}, [])
    task = store.pending('a')
    assert task['deep_owner'] == 'discovery'
    monkeypatch.setattr(store, 'discovery_mode', lambda db=None:0)
    answer = {'feedback':[{'event_id':'event','judgment':'helpful','reason':'addresses the question'}]}
    with monkeypatch.context() as m:
        def broken(*args):
            raise RuntimeError('write fault')
        m.setattr(store.questions,'submit_one',broken)
        with pytest.raises(RuntimeError):
            store.finish_discovery_evaluation(task, answer, {'event'}, question())
    assert store.pending('a')['stage'] == 'evaluate'
    with store.connect() as db:
        assert not db.execute('SELECT 1 FROM feedback').fetchone()
    assert store.finish_discovery_evaluation(task, answer, {'event'}, question())
    store.link_run_outcome('run', {}, [])
    store.fail_run('run', 'late retry')
    assert store.pending('a') is None
    assert store.questions.has_pending('a')
    assert not store.finish_discovery_evaluation(task, answer, {'event'}, question())
    restarted = ReflectionMemoryStore(tmp_path / 'q.sqlite')
    pending = restarted.pending_projection('a')
    projection = restarted.claim_projection('a', pending['id'])
    assert not store.claim_projection('a', pending['id'])
    assert json.loads(projection['evaluation'])['reviewed_ids'] == ['event']
    restarted.finish_projection('a', projection, success=False)
    retry = store.claim_projection('a', pending['id'])
    restarted.finish_projection('a', projection, success=True)
    store.finish_projection('a', retry, success=False)
    assert restarted.pending_projection('a')
    restarted.finish_projection('a', restarted.claim_projection('a', pending['id']), success=True)
    assert not store.pending_projection('a')
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM feedback').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM inner_runs').fetchone()[0] == 1


def test_upgrade_preserves_old_owner_and_stale_host_obeys_route(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.sqlite'
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE observations(id INTEGER PRIMARY KEY,subject TEXT,query TEXT,object_id TEXT,"
                    "payload TEXT,created REAL,stage TEXT DEFAULT 'evaluate')")
        old.execute("INSERT INTO observations VALUES(1,'a','q','','[]',1,'reprocess')")
    store = ReflectionMemoryStore(path)
    assert store.pending('a')['deep_owner'] == 'legacy'
    snapshot = ParamSnapshot(2, {'INNER_ACTIVITY_MODE':2}, tmp_path/'overlay.json')
    monkeypatch.setattr('jshi.core.param_overlay.current_snapshot',lambda:snapshot)
    monkeypatch.setattr('jshi.reflection.memory_store.param',lambda name:2)
    assert store.discovery_mode() == 2
    snapshot = replace(snapshot, version=1)
    monkeypatch.setattr('jshi.reflection.memory_store.param',lambda name:0)
    assert ReflectionMemoryStore(path).discovery_mode() == 2
    snapshot = replace(snapshot, version=3)
    assert store.discovery_mode() == 0
