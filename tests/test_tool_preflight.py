import asyncio
import json
from types import SimpleNamespace

import pytest

from jshi.tool import ToolRequest, AskMode
from jshi.tool.contract import Budget
from jshi.tool.preflight import preview
from jshi.app.tool_preflight import EstimateBridge
from test_tool_work_reuse import runtime, launch


def test_unknown_price_is_not_free_or_confirmation():
    estimate = preview(ToolRequest(), {'cost_est': 0, 'observed_runs': 3, 'time_est_ms': 1200})
    assert estimate['cost_est'] is None
    assert not estimate['need_confirm']
    assert '未知' not in estimate['currency'] and '不代表免费' in estimate['text']
    assert '1.2秒' in estimate['text']


@pytest.mark.parametrize('estimate,budget', [
    ({'need_confirm': True}, Budget()),
    ({'time_est_ms': 2000}, Budget(timeout_ms=1000)),
    ({'cost_est': 2, 'cost_currency': 'USD'}, Budget(cost_cap=1)),
])
def test_expensive_and_over_budget_wait_before_execution(tmp_path, estimate, budget):
    service, engine = runtime(tmp_path)
    notices = []
    def listener(subject, owner, tid, data):
        assert not engine.calls
        assert tid in service._proposed_requests
        notices.append(data)
    service.subscribe_estimates(listener)
    tid = launch(service, ToolRequest(command='weather', budget=budget, meta={'estimate': estimate}))
    assert not engine.calls and notices[0]['need_confirm']
    assert service.intake_store.get_by_task_id(tid).status == 'proposed'
    assert service.approve(tid)
    service.drain_for_tests()
    assert len(engine.calls) == 1 and engine.calls[0].ask is AskMode.EXECUTE
    assert not service.approve(tid)


def test_normal_announced_before_execution_and_metadata_survives(tmp_path):
    service, engine = runtime(tmp_path)
    seen = []
    def listener(subject, owner, tid, data):
        assert not engine.calls
        seen.append(data)
    service.subscribe_estimates(listener)
    tid = launch(service, ToolRequest(command='weather', expected_result='verified weather',
        meta={'estimate': {'time_est_ms': 1000, 'cost_est': .01, 'cost_currency': 'CNY', 'basis': 'price table'}}))
    assert len(engine.calls) == 1 and seen[0]['cost_est'] == .01
    hang = service.hang_store.get(tid)
    assert hang.meta['expected_result'] == 'verified weather'
    assert json.loads(hang.meta['preflight'])['currency'] == 'CNY'
    entries = service.list_tool_related_entries('s', 'o', required_ids=(tid,))
    assert 'price table' in entries[0]['preflight']
    service.unsubscribe_estimates(listener)
    assert not service.estimate_listeners


def test_bridge_filters_owner_subject_and_rejects_unannounced_approval():
    async def run():
        listeners, messages, approved = set(), [], []
        async def emit(kind, **data):
            messages.append((kind, data))
        def approve(tid):
            approved.append(tid)
            return True
        service = SimpleNamespace(subscribe_estimates=listeners.add, unsubscribe_estimates=listeners.discard, approve=approve)
        c = SimpleNamespace(subject_id='s', closed=False, output_disconnected=False,
            scene=[SimpleNamespace(speaker=SimpleNamespace(object_id='o', status='recognized'))], emit=emit)
        bridge = EstimateBridge(c, service)
        data = {'text': 'estimate', 'need_confirm': True}
        try:
            await asyncio.to_thread(bridge.announce, 'other', 'o', 'bad1', data)
            await asyncio.to_thread(bridge.announce, 's', 'other', 'bad2', data)
            await bridge.approve('bad1')
            assert not messages and not approved
            await asyncio.to_thread(bridge.announce, 's', 'o', 'good', data)
            assert messages[0][0] == 'tool_estimate'
            await bridge.approve('good')
            await bridge.approve('good')
            assert approved == ['good']
        finally:
            await bridge.close()
        assert not listeners and not bridge.proposals and not bridge.pending
    asyncio.run(run())


def test_confirmed_creation_keeps_followup_registration(tmp_path):
    from jshi.tool.contract import CreateToolSpec
    service, engine = runtime(tmp_path)
    tid = launch(service, ToolRequest(create=CreateToolSpec(tool_name='new-tool'),
        meta={'estimate': {'need_confirm': True}}))
    starts = []
    def start(request, task_id):
        assert task_id in service._create_tasks
        starts.append(task_id)
    service.runner.start = start
    assert service.approve(tid) and starts == [tid]


def test_cancellation_during_estimate_delivery_does_not_start(tmp_path):
    service, engine = runtime(tmp_path)
    def cancel(subject, owner, tid, data):
        service.cancel(tid)
    service.subscribe_estimates(cancel)
    launch(service, ToolRequest(command='weather'))
    assert not engine.calls


def test_planner_preserves_estimate_evidence_and_currency():
    from jshi.skill.tool_plan import ToolPlanSkill
    skill = ToolPlanSkill(object())
    parsed = skill.parse({'ok': True, 'ask': 'execute', 'command': 'weather',
        'estimate': {'cost_est': .2, 'cost_currency': 'CNY', 'time_est_ms': 3000,
                     'basis': 'documented price', 'need_confirm': False}})
    assert parsed.meta['estimate']['basis'] == 'documented price'
    assert parsed.meta['estimate']['cost_currency'] == 'CNY'
    assert 'cost_currency' in skill.system_extra()


def test_bridge_close_cancels_inflight_delivery_and_releases_worker():
    async def run():
        listeners = set()
        started = asyncio.Event()
        async def emit(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        service = SimpleNamespace(subscribe_estimates=listeners.add, unsubscribe_estimates=listeners.discard)
        c = SimpleNamespace(subject_id='s', closed=False, output_disconnected=False,
            scene=[SimpleNamespace(speaker=SimpleNamespace(object_id='o', status='recognized'))], emit=emit)
        bridge = EstimateBridge(c, service)
        worker = asyncio.create_task(asyncio.to_thread(bridge.announce, 's', 'o', 't', {'text':'estimate', 'need_confirm':True}))
        try:
            await asyncio.wait_for(started.wait(), 1)
        finally:
            await bridge.close()
            await asyncio.wait_for(worker, 1)
        assert not bridge.pending and not listeners
    asyncio.run(run())
