import asyncio
import json
import threading

from jshi.decision.service import DecisionService
from jshi.decision.base import DecisionResult


def test_partial_nested_settings_survive_load_and_save(tmp_path):
    (tmp_path/'decision_settings.local.json').write_text(json.dumps({
        'provider':'typesafe', 'providers':{'typesafe':{'model':'custom'}},
        'entry_thresholds':{'typesafe':{'stop':.97}}}), encoding='utf8')
    service = DecisionService(tmp_path)
    assert service.config['provider'] == 'typesafe'
    assert service.config['providers']['typesafe']['model'] == 'custom'
    assert service.config['entry_thresholds']['typesafe']['stop'] == .97
    service.save({'entry_thresholds':{'typesafe':{'wait':.96}}})
    assert service.config['entry_thresholds']['typesafe']['stop'] == .97
    assert service.config['entry_thresholds']['typesafe']['wait'] == .96
    assert 'bailian' in service.config['providers']


def test_slow_disk_does_not_delay_decision(tmp_path):
    class Port:
        def __init__(self, *args): pass
        async def decide(self, request, *, deadline):
            return DecisionResult('success')
        async def close(self): pass
    async def scenario():
        service = DecisionService(tmp_path, port_factory=Port)
        service.config['mode'] = 'active'
        release = threading.Event()
        writes = []
        def record(value):
            release.wait(2)
            writes.append(value['kind'])
        service.record = record
        try:
            result = await asyncio.wait_for(service.decide('entry', {}), .3)
            assert result.status == 'success'
            assert not service.cooldown
            for i in range(100): service.audit({'kind':'extra'})
            assert len(service.audit_tasks) <= 64 and service.audit_dropped > 0
        finally:
            release.set()
            await service.close()
        assert writes[:2] == ['request', 'result']
    asyncio.run(scenario())


def test_candidate_with_preparation_reference_is_not_reused():
    from jshi.subject.preparation import PreparedAnalysis
    value = PreparedAnalysis('s', '', (), 1, {'candidate':'依据R1的约定'})
    assert not value.compatible('s', '', (), 1)
    value.content = {'candidate':'可以在正式材料中重新核对约定'}
    assert value.compatible('s', '', (), 1)


def test_shadow_observation_backlog_is_bounded(tmp_path):
    from jshi.voice.jev_decision import entry_bridge
    async def scenario():
        service = DecisionService(tmp_path)
        service.config['mode'] = 'shadow'
        hold = asyncio.Event()
        async def decide(*args, **kwargs):
            await hold.wait()
            return DecisionResult('success')
        service.decide = decide
        bridge = entry_bridge(service)
        await asyncio.gather(*(asyncio.to_thread(bridge, {}) for _ in range(20)))
        assert len(service.observations) == 1
        await service.close()
    asyncio.run(scenario())
