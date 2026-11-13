from jshi.tool.work import WorkIndex
from jshi.tool import HangStore, StubEngine, ToolModule, ToolRunner, ToolService


def test_budget_persists_deduplicates_and_stops_after_three_failures(tmp_path):
    index = WorkIndex(tmp_path / 'work.jsonl')
    index.create('w', 's', 'p', 'query')
    for i in range(3):
        index.record_attempt('w', str(i), success=False, time_ms=1000, reason='timeout')
        index.record_attempt('w', str(i), success=False, time_ms=1000)
    row = WorkIndex(index.path).get('w')
    budget = row['execution_budget']
    assert len(budget['attempts']) == 3 and budget['elapsed_ms'] == 3000
    assert budget['consecutive_failures'] == 3 and budget['stop_reason']
    assert budget['unknown_cost_attempts'] == 3
    assert not row['closed']
    assert 'timeout' in index.budget_note(row)


def test_success_resets_failure_streak_but_not_total_time(tmp_path):
    index = WorkIndex(tmp_path / 'work.jsonl')
    index.create('w', 's', 'p', 'query')
    index.record_attempt('w', 'a', success=False, time_ms=200000)
    index.record_attempt('w', 'b', success=True, time_ms=200000, cost=0)
    row = index.get('w')['execution_budget']
    assert row['consecutive_failures'] == 0 and not row['stop_reason']
    index.record_attempt('w', 'c', success=False, time_ms=200000)
    assert '10分钟' in index.get('w')['execution_budget']['stop_reason']


def test_exhausted_work_is_reported_and_not_dispatched(tmp_path):
    hang = HangStore(tmp_path / 'hang.jsonl')
    service = ToolService(hang, ToolRunner(ToolModule(StubEngine()), hang))
    record = service.intake_store.create(subject_id='s', object_id='p', need='query')
    service.work_index.create('w', 's', 'p', 'query')
    record = service.intake_store.update(record.intake_id, meta={'work_id':'w'})
    for i in range(3):
        service.work_index.record_attempt('w', str(i), success=False, reason='failed')
    assert not service._intake_current(record)
    failed = service.intake_store.get(record.intake_id)
    assert failed.status == 'failed' and '停止后续尝试' in failed.plan_error
    assert service.work_index.pending_notices('s', 'p')


def test_planning_failures_also_accumulate_without_counting_budget_refusal(tmp_path):
    hang = HangStore(tmp_path / 'hang.jsonl')
    service = ToolService(hang, ToolRunner(ToolModule(StubEngine()), hang))
    service.work_index.create('w', 's', 'p', 'query')
    for _ in range(3):
        record = service.intake_store.create(subject_id='s', object_id='p', need='query')
        service.intake_store.update(record.intake_id, meta={'work_id':'w'})
        service._fail_intake(record.intake_id, 'model failed')
    assert service.work_index.get('w')['execution_budget']['consecutive_failures'] == 3
    record = service.intake_store.create(subject_id='s', object_id='p', need='query')
    record = service.intake_store.update(record.intake_id, meta={'work_id':'w'})
    assert not service._intake_current(record)
    assert len(service.work_index.get('w')['execution_budget']['attempts']) == 3
