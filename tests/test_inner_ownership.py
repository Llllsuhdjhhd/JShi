from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from jshi.core.param_overlay import override_snapshot
from jshi.reflection import (InProcessReflection, IntrospectionQueue, IntrospectionRequest,
                             IntrospectionStatus, IntrospectionTrigger, MOMENT_AFTER_ACTIVITY)
from jshi.reflection.discovery import RuleDiscoveryAssistant, signal_context
from jshi.subject import HistoryKind, HistoryRecord
from jshi.tool.contract import FeedbackKind, ToolFeedback, ToolResult, ToolStatus
from test_introspection import runtime


def test_pending_old_idle_is_recovered_and_new_idle_survives_rollback(tmp_path):
    process, repo = runtime(tmp_path)
    inner = process.reflection_executor.inner
    ledger = process.activity_ledger
    now = datetime.now(timezone.utc)
    old = ledger.append_external('stone', actor_object_id='OBJ-USER', text_raw='我想先读完这一本书。', occurred_at=now)
    legacy = process.reflection.enqueue_idle('stone')
    assert legacy and legacy.entry_ref == 'segment:' + old.segment_id
    fresh = ledger.append_external('stone', actor_object_id='OBJ-USER', text_raw='我接下来还想看看别的书。', occurred_at=now)
    with override_snapshot({'INNER_ACTIVITY_MODE': 2}):
        found = inner.scan('stone')
        assert len(found) == 1 and found[0].evidence['segment_ids'] == [fresh.segment_id]
        # Simulate loss of the original process-local queue.
        replacement = InProcessReflection(process)
        assert replacement.drain('stone')[0].request.request_id == legacy.request_id
        assert not process.reflection.drain('stone')
    assert process.reflection.enqueue_idle('stone') is None
    with override_snapshot({'INNER_ACTIVITY_MODE': 2}):
        assert not inner.scan('stone')
        assert len(inner.drain('stone')) == 1
    done = [r.content['entry_ref'] for r in repo.list_history('stone') if r.event_type == 'introspection_done']
    assert sorted(done) == sorted(['segment:' + old.segment_id, 'segment:' + fresh.segment_id])


def test_real_tool_result_via_old_and_new_paths_and_later_receipt(tmp_path):
    process, _ = runtime(tmp_path)
    inner = process.reflection_executor.inner
    hang = process.hang_store
    task = hang.create(subject_id='stone', object_id='OBJ-USER', need='核对时间', field_ref={'activity_id':'A1'})
    activity = SimpleNamespace(id='A1', updated_at=datetime.now(timezone.utc))
    def receipt(summary):
        hang.append_feedback(task.task_id, [ToolFeedback(kind=FeedbackKind.RESULT,
                             result=ToolResult(status=ToolStatus.FAILED, summary=summary))])
    def old_enqueue():
        return process.reflection.enqueue_from_sources(MOMENT_AFTER_ACTIVITY, 'stone',
                                                       activity=activity, object_id='OBJ-USER')
    receipt('第一次核对未得到结果')
    first, = old_enqueue()
    assert first.evidence['source_versions']
    with override_snapshot({'INNER_ACTIVITY_MODE': 2}):
        assert not inner.scan('stone')
        receipt('复核时服务繁忙')
        # Foreground accepts the new receipt; roll back before discovery consumes it.
        assert inner.questions.has_signals('stone')
    assert not old_enqueue()
    with override_snapshot({'INNER_ACTIVITY_MODE': 2}):
        second, = inner.scan_signals('stone')
        assert second.evidence['source_versions'] != first.evidence['source_versions']
        assert '服务繁忙' in second.evidence['tool_facts'][0]['summary']
        inner.scan('stone')
    with inner.store.connect() as db:
        assert db.execute('SELECT count(*) FROM inner_runs').fetchone()[0] == 1


def test_upgrade_recovers_only_complete_unambiguous_original_sources(tmp_path):
    process, repo = runtime(tmp_path)
    inner = process.reflection_executor.inner
    now = datetime.now(timezone.utc)
    row = HistoryRecord('stone', HistoryKind.SUBJECT, 'epistemic_transition',
                        {'cognitive_content_id':'c', 'from':'considering', 'to':'revised'}, created_at=now)
    repo.add_history(row)
    for subject, anchor in [('stone','s'), ('other','foreign')]:
        repo.add_history(HistoryRecord(subject, HistoryKind.SUBJECT, 'introspection_done',
                         {'trigger':'idle','entry_ref':'segment:' + anchor,'entry_at':now.isoformat()}))
    repo.add_history(HistoryRecord('stone', HistoryKind.SUBJECT, 'introspection_done',
                     {'trigger':'epistemic_revised','entry_ref':'cognition:c','entry_at':now.isoformat()}))
    repo.add_history(HistoryRecord('stone', HistoryKind.SUBJECT, 'introspection_done',
                     {'trigger':'event_result','entry_ref':'activity:A1','entry_at':now.isoformat()}))
    request = IntrospectionRequest('stone', IntrospectionTrigger.EPISTEMIC_REVISED, 'cognition:c', now,
                                   evidence={'transition_ref':'transition:' + row.id})
    with override_snapshot({'INNER_ACTIVITY_MODE': 2}):
        inner.questions.put_signals('stone', (request,))
        assert not inner.scan_signals('stone')
        assert not inner.questions.has_pending('stone')
        keys = ['segment:s','segment:foreign', 'transition:' + row.id,'activity:A1']
        assert inner.questions.covered('stone', keys, owner='legacy') == {keys[0], keys[2]}
        changed = replace(request, evidence={'transition_ref':'transition:next'})
        new_question, = RuleDiscoveryAssistant().discover(signal_context('stone', (changed,))).requests
        assert inner.questions.submit('stone', (new_question,))
        assert process.reflection.enqueue(changed) == IntrospectionStatus.DROPPED


def test_competing_old_and_new_receipt_has_one_owner(tmp_path):
    process, _ = runtime(tmp_path)
    store = process.reflection_memory
    request = IntrospectionRequest('stone', IntrospectionTrigger.EVENT_RESULT, 'activity:a',
                                   datetime.now(timezone.utc), evidence={'source_versions':['tool:t:e:v1']})
    queue = IntrospectionQueue()
    with ThreadPoolExecutor(2) as pool:
        old = pool.submit(store.questions.enqueue_legacy, request, queue)
        new = pool.submit(store.questions.submit, 'stone', (request,))
        assert new.result()
        legacy_won = old.result().status == IntrospectionStatus.QUEUED
    assert store.questions.legacy_pending('stone') == legacy_won
    assert store.questions.has_pending('stone') != legacy_won
