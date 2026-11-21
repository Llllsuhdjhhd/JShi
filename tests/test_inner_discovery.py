import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jshi.experienceledger.inprocess import InProcessExperienceLedger
from jshi.experienceledger.sqlite import SqliteExperienceLedger
from jshi.reflection.discovery import (RuleDiscoveryAssistant, read_discovery_context,
                                       requests_for_tool_record, signal_context)
from jshi.reflection.port import IntrospectionRequest, IntrospectionTrigger
from jshi.tool.contract import FeedbackKind, ToolFeedback, ToolProgress, ToolResult, ToolStatus
from jshi.tool.hang import HangStore


DAY = datetime(2026, 11, 21, 10, tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture(params=['memory', 'sqlite'])
def ledger(request, tmp_path):
    return SqliteExperienceLedger(tmp_path/'ledger.db') if request.param == 'sqlite' else InProcessExperienceLedger()


def test_discovery_pages_and_scoped_anchors(ledger):
    def event(who, text, at=DAY, subject='stone'):
        return ledger.append_external(subject, actor_object_id=who, text_raw=text, occurred_at=at)

    prior = event('lin', '上周约在阅览室。', DAY-timedelta(days=7))
    event('other', '他人的旧经历', DAY-timedelta(days=2))
    event('lin', '太久以前的事情', DAY-timedelta(days=8))
    first = event('lin', '我们能先到分馆吗？')
    own = ledger.append_subject_reply('stone', text_raw='你想早点安静下来读书吗？', occurred_at=DAY)
    other = event('other', '下午还要商量另一件事。')
    foreign = event('lin', '其他主体', subject='someone')
    process = SimpleNamespace(activity_ledger=ledger)
    ctx = read_discovery_context(process, 'stone', 'ledger', 0, now=DAY, limit=2)
    assert ctx.segments == (first, own)
    assert ctx.baseline == (prior,)
    batch = RuleDiscoveryAssistant().discover(ctx)
    assert batch.requests[0].evidence['segment_ids'] == [first.segment_id, own.segment_id]
    assert '分馆' in batch.requests[0].entry_text
    assert set(batch.coverage[0]['seen']) == {first.segment_id, own.segment_id}
    late = event('lin', '今天才补记的一段旧交谈', DAY-timedelta(days=12))
    follow = read_discovery_context(process, 'stone','ledger',ctx.next_cursor,now=DAY+timedelta(days=1),limit=2)
    assert follow.segments == (other, late)
    assert ledger.experiences_by_ids('stone',[other.segment_id, first.segment_id, foreign.segment_id]) == (other, first)
    person = read_discovery_context(process,'stone','ledger',0,now=DAY,object_id='other')
    assert person.segments == (other,)
    assert all(r.actor_object_id == 'other' for r in person.baseline)


def test_empty_batch_and_persisted_cross_process_page(tmp_path):
    ledger = SqliteExperienceLedger(tmp_path/'store.db')
    fresh = SqliteExperienceLedger(tmp_path/'store.db')
    process = SimpleNamespace(activity_ledger=fresh)
    first = ledger.append_external('stone',actor_object_id='lin',text_raw='好。',occurred_at=DAY)
    context = read_discovery_context(process,'stone','ledger',0,now=DAY)
    batch = RuleDiscoveryAssistant().discover(context)
    assert context.next_cursor > 0 and not batch.requests
    assert batch.coverage[0]['seen'] == [first.segment_id]
    second = ledger.append_external('stone',actor_object_id='lin',text_raw='现在该去哪个分馆？',occurred_at=DAY)
    assert fresh.experiences_by_ids('stone',[second.segment_id]) == (second,)


def test_empty_first_visit_allows_late_arrival_after_midnight(ledger):
    process = SimpleNamespace(activity_ledger=ledger)
    initial = read_discovery_context(process, 'stone', 'ledger', 0, now=DAY)
    assert initial.next_cursor > 0 and not initial.segments
    late = ledger.append_external('stone', actor_object_id='lin', text_raw='还有一条当晚才记下的事。',
                                  occurred_at=DAY + timedelta(hours=6))
    following = read_discovery_context(process, 'stone', 'ledger', initial.next_cursor,
                                       now=DAY + timedelta(days=1))
    assert following.segments == (late,)


def test_every_group_and_its_own_baseline_are_examined(ledger):
    def event(who, text, at=DAY):
        return ledger.append_external('stone', actor_object_id=who, text_raw=text, occurred_at=at)

    past = event('feng', '出发之前总要再查一次时刻表。', DAY - timedelta(days=1))
    first = event('lin', '我想试试骑车这条路。')
    second = event('feng', past.text_raw)
    third = event('lin', '原来河边也有一条小道。')
    own = ledger.append_subject_reply('stone', text_raw='你接着说。', occurred_at=DAY)
    process = SimpleNamespace(activity_ledger=ledger)
    assistant = RuleDiscoveryAssistant()
    page = read_discovery_context(process, 'stone', 'ledger', 0, now=DAY, limit=3)
    batch = assistant.discover(page)
    b, ordinary = batch.requests
    assert b.evidence['segment_ids'] == [second.segment_id, past.segment_id]
    assert b.evidence['method'] == 'compare' and ordinary.evidence['baseline_status'] == 'unknown'
    coverage = batch.coverage[0]
    assert len(coverage['selected']) == 2 and len(coverage['reviewed_only']) == 1
    assert set(coverage['selected'] + coverage['reviewed_only']) == {
        'segment:' + r.segment_id for r in (first, second, third)}
    next_page = read_discovery_context(process, 'stone', 'ledger', page.next_cursor, now=DAY, limit=3)
    continued, = assistant.discover(next_page).requests
    assert continued.dedup_key == 'ordinary:' + third.segment_id
    assert continued.evidence['segment_ids'] == [third.segment_id, own.segment_id]
    scoped = read_discovery_context(process, 'stone', 'ledger', 0, now=DAY, object_id='lin')
    assert {r.dedup_key for r in assistant.discover(scoped).requests} == {
        'ordinary:' + r.segment_id for r in (first, third)}


def test_future_receipt_does_not_hide_later_arrivals_or_become_visible_early(ledger, tmp_path):
    from jshi.reflection.inner_runtime import InnerActivityRuntime
    from jshi.reflection.memory_store import ReflectionMemoryStore

    process = SimpleNamespace(activity_ledger=ledger, repository=None)
    def runtime():
        return InnerActivityRuntime(process, object(), ReflectionMemoryStore(tmp_path / 'questions.db'))
    def event(who, at):
        return ledger.append_external('stone', actor_object_id=who, text_raw=f'{who} 说下午要改期。', occurred_at=at)
    later = event('lin', DAY + timedelta(minutes=1))
    distant = event('far', DAY + timedelta(days=365))
    current = event('feng', DAY)
    inner = runtime()
    request, = inner.scan('stone', now=DAY)
    assert request.evidence['segment_ids'] == [current.segment_id]
    with inner.store.connect() as db:
        coverage = json.loads(db.execute("SELECT coverage FROM inner_scans WHERE source='ledger'").fetchone()[0])[0]
    assert set(coverage['deferred']) == {later.segment_id, distant.segment_id}
    assert coverage['seen'] == [current.segment_id]
    assert not inner.scan('stone', now=DAY)
    inner = runtime()
    recovered, = inner.scan('stone', now=DAY + timedelta(minutes=2))
    assert recovered.evidence['segment_ids'] == [later.segment_id]
    assert not inner.scan('stone', now=DAY + timedelta(days=1))
    assert inner.questions.due_segments('stone', 'ledger', now=(DAY + timedelta(days=366)).timestamp()) == (
        distant.segment_id,)


def test_legacy_tool_facts_survive_reload_without_inventing_an_event_time(tmp_path):
    path = tmp_path / 'old.jsonl'
    raw = {'task_id':'t', 'subject_id':'stone', 'object_id':'lin', 'need':'查时间',
           'created_at':DAY.isoformat(), 'updated_at':DAY.isoformat(), 'feedback':[
               {'kind':'result', 'result':{'status':'ok', 'summary':'不知道何时收到'}},
               {'kind':'result', 'at':DAY.isoformat(), 'result':{'status':'ok', 'summary':'有记录'}}]}
    path.write_text(json.dumps(raw), encoding='utf-8')
    def read():
        store = HangStore(path)
        return [(pos, requests_for_tool_record('stone', record, [item]))
                for pos, record, item in store.read_result_page('stone')]
    missing, known = read()
    assert not missing[1] and known[1][0].entry_at == DAY
    again_missing, again_known = read()
    assert again_missing == missing and again_known[0] == known[0]
    assert again_known[1][0].evidence == known[1][0].evidence


def test_ordinary_tool_sampling_replays_the_same_choice_and_tracks_recovery(tmp_path):
    from jshi.reflection.discovery import DiscoveryContext

    record = HangStore(tmp_path / 'hang.jsonl').create(subject_id='stone', object_id='lin', need='查时间')
    ok = ToolFeedback(at=DAY, result=ToolResult(summary='18:00'))
    bad = ToolFeedback(at=DAY, result=ToolResult(status=ToolStatus.FAILED, summary='未找到'))
    assistant = RuleDiscoveryAssistant()
    choices = set()
    for index in range(12):
        task = replace(record, task_id=f'task-{index}', feedback=(ok,))
        immediate = signal_context('stone', requests_for_tool_record('stone', task, [ok]))
        # A failure after the old success must not change that old selection.
        later = replace(task, feedback=(ok, bad))
        replay = DiscoveryContext('stone', 'tools', DAY, tool_results=((later, ok),))
        selected = bool(assistant.discover(immediate).requests)
        assert selected == bool(assistant.discover(replay).requests)
        choices.add(selected)
    assert choices == {True, False}
    recovered = replace(record, feedback=(bad, ok))
    for item in (bad, ok):
        context = DiscoveryContext('stone', 'tools', DAY, tool_results=((recovered, item),))
        assert assistant.discover(context).requests


def test_tool_result_receipt_paged_late_replayed_and_activity_close_share_one_fact(tmp_path):
    path = tmp_path/'hang.jsonl'
    store = HangStore(path)
    def task(subject, activity):
        return store.create(subject_id=subject,object_id='lin',need='查分馆时间',field_ref={'activity_id':activity})
    t1 = task('stone','a1')
    t2 = task('stone','a2')
    progress = ToolFeedback(kind=FeedbackKind.PROGRESS, progress=ToolProgress(partial='继续查询'))
    bad = ToolFeedback(at=DAY,kind=FeedbackKind.RESULT,result=ToolResult(status=ToolStatus.FAILED,summary='未核验'))
    ok = ToolFeedback(at=DAY,kind=FeedbackKind.RESULT,result=ToolResult(summary='已核验'))
    called = []
    class Listener:
        def observe(self, record, items):
            called.append((store.get(record.task_id).task_id,items))
    listener = Listener()
    store.add_result_listener(listener.observe)
    store.add_result_listener(listener.observe)
    store.append_feedback(t1.task_id,[progress])
    assert not called
    store.append_feedback(t2.task_id,[ok])
    first = store.read_result_page('stone',limit=1)
    store.append_feedback(t1.task_id,[bad])
    assert called == [(t2.task_id,(ok,)),(t1.task_id,(bad,))]
    point = first[0][0]
    reloaded = HangStore(path)
    reloaded.person_links = SimpleNamespace(project=lambda subject, owner, refs: 'projected-lin')
    assert reloaded.get(t1.task_id).object_id == 'projected-lin'
    after = read_discovery_context(SimpleNamespace(hang_store=reloaded),'stone','tools',point,now=DAY)
    assert after.next_cursor > point and after.tool_results[0][0].task_id == t1.task_id
    assert not reloaded.read_result_page('other')
    seed = IntrospectionRequest(subject_id='stone',object_id='projected-lin',trigger=IntrospectionTrigger.EVENT_RESULT,
                                entry_ref='activity:a1',entry_at=DAY,evidence={'tool_failures':[{}]})
    close = signal_context('stone',[seed],hang_store=reloaded,now=DAY)
    scan = RuleDiscoveryAssistant().discover(after).requests[0]
    at_close = RuleDiscoveryAssistant().discover(close).requests[0]
    immediate = requests_for_tool_record('stone',t1,[progress,bad])[0]
    assert immediate.object_id == at_close.object_id == scan.object_id == 'lin'
    assert immediate.dedup_key == at_close.dedup_key == scan.dedup_key
    assert immediate.evidence['source_versions'] == at_close.evidence['source_versions'] == scan.evidence['source_versions']
    assert reloaded.get(t1.task_id).delivered_at is None
