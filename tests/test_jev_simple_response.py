import pytest
import asyncio
from aiohttp.test_utils import TestClient, TestServer

from jshi.voice.direct_reply import DirectReplyState
from jshi.voice.wait_ack import WaitAckTiming
from jshi.voice.jev import VoiceJEV
from jshi.voice.config import VoiceConfig
from jshi.app.voice import create_app
from tests.test_jev_split_modes import run, entry, preparation, Model
from tests.test_voice import process


def test_general_answer_needs_no_person_history_but_does_not_create_it():
    state = DirectReplyState()
    assert not state.context('s', ['p'])['allowed']
    material = state.context('s', ['p'], allow_general=True)
    assert material['allowed'] and material['person'] is None
    assert not state.context('s', ['p', 'q'], allow_general=True)['allowed']
    assert not state.context('s', [None], allow_general=True)['allowed']
    assert state.accept('s', 'p', '星期六英语怎么说', 'Saturday', allow_general=True)
    assert not state.people  # No invented identity/history cache.


@pytest.mark.parametrize('mode', ['split', 'combined'])
@pytest.mark.parametrize('state,interaction,overlap,expected', [
    ('completed', 'direct', False, True),
    ('thinking', 'direct', False, False),
    ('playing', 'direct', False, False),
    ('paused', 'direct', False, False),
    ('completed', 'uncertain', False, False),
    ('completed', 'direct', True, False),
])
def test_idle_keep_can_answer_but_does_not_bypass_address_or_busy_checks(mode, state, interaction, overlap, expected):
    result, _ = run(mode, entry(control='keep', interaction=interaction),
        preparation(route='self_reply', level=3, reply='星期六是Saturday。'),
        delivery={'state': state, 'short_chat': {'allowed': True, 'person_code': 'P1', 'person': None}},
        overlap=overlap)
    assert bool(result.direct_reply) == expected


@pytest.mark.parametrize('seconds,interaction,state,expected', [
    (1.0, 'direct', 'completed', False),
    (1.5, 'direct', 'completed', True),
    (3.5, 'direct', 'completed', True),
    (3.5, 'uncertain', 'completed', False),
    (3.5, 'direct', 'thinking', False),
])
def test_ack_budget_and_address_are_enforced(tmp_path, seconds, interaction, state, expected):
    timing = WaitAckTiming(tmp_path / 'timing.json')
    timing.observe('model', 2, seconds)
    result, _ = run('split', entry(control='keep', interaction=interaction),
        preparation(waiting_reply='嗯。'), delivery={'state': state,
            'waiting': timing.guidance('model', allowed=True)})
    assert bool(result.waiting_reply) == expected


def test_first_turn_split_short_answer_reaches_delivery_without_main(process):
    model = Model([entry(control='keep'), preparation(route='self_reply', level=3, reply='星期六是Saturday。')])
    async def check():
        before = len(process.cognition.requests)
        async with TestClient(TestServer(create_app(process, 'stone', VoiceConfig(api_key=''),
                jev=VoiceJEV(model, mode='split')))) as client:
            response = await client.post('/text-input', headers={'Origin': str(client.make_url('')).rstrip('/')},
                json={'text': '匠石，星期六英语怎么说？', 'object_id': 'lux-id'})
            assert response.status == 200
            body = await response.json()
            assert body['route'] == 'self_reply' and body['reply'] == '星期六是Saturday。'
            assert len(process.cognition.requests) == before
            assert '仅可用当前材料或通用知识短答' in model.requests[1].input_text
            assert '"activity_state":"completed"' in model.requests[1].input_text
    asyncio.run(check())
