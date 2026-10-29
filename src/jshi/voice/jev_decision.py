"""Loop-owned decision transport exposed to bounded synchronous JEV workers."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy
from time import monotonic

from jshi.decision.base import DecisionResult


def entry_bridge(service):
    """Create on the host loop; invoke only in a worker. Never create an HTTP loop."""
    loop = asyncio.get_running_loop()
    config = deepcopy(service.config)
    tasks = service.observations

    async def request(state, budget_ms=None):
        snapshot = deepcopy(config)
        if budget_ms is not None:
            snapshot['entry_timeout_ms'] = min(snapshot['entry_timeout_ms'], budget_ms)
        return await service.decide('entry', state, config_snapshot=snapshot)

    def decide(state, *, deadline=None):
        if config['mode'] == 'off' or not config.get('entry', True):
            return DecisionResult('unavailable', reason='entry_disabled'), config
        if config['mode'] == 'shadow':
            # Spawn observation on the owning loop. Its result never reaches routing.
            def observe():
                if service.slots.locked() or tasks:
                    return
                task = loop.create_task(request(state))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            loop.call_soon_threadsafe(observe)
            return DecisionResult('unavailable', reason='shadow'), config
        budget = config.get('entry_timeout_ms', 800)/1000
        if deadline is not None:
            budget = min(budget, deadline - monotonic())
        if budget <= 0:
            return DecisionResult('timeout', reason='batch_deadline'), config
        future = asyncio.run_coroutine_threadsafe(request(state, max(1, int(budget*1000))), loop)
        try:
            return future.result(budget), config
        except FutureTimeout:
            future.cancel()
            return DecisionResult('timeout', reason='bridge_deadline'), config
    def report(actual, reason):
        value = {'selected': 'decision', 'actual': actual, 'reason': reason,
                 'provider': config['provider'], 'mode': config['mode']}
        loop.call_soon_threadsafe(setattr, service, 'entry_last', value)
    decide.report = report
    return decide
