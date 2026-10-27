import asyncio
import json
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jshi.app.voice import VoiceConversation, create_app
from jshi.core.envelope import InputEnvelope, InputPart, SceneUtterance, SpeakerEvidence
from jshi.models import ModelResponse
from jshi.voice.config import VoiceConfig
from jshi.voice.direct_reply import DirectReplyState, reply_state
from jshi.voice.jev import VoiceJEV
from tests.test_voice import process, FakeCloud


class ShortChatModel:
    def __init__(self, **changes):
        self.changes = changes
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        lines = json.loads(request.input_text)['input']['lines']
        output = {'items':[{'i':r['i'], 'person':r['p'], 'certainty':'confirmed',
                           'why':'当前人物由用户选择'} for r in lines],
                  'level':3, 'recall_memory':False, 'control':'redirect', 'route':'self_reply', 'reply':'嗯，我在。'}
        output.update(self.changes)
        return ModelResponse(model='fake', text=json.dumps(output))


@pytest.mark.parametrize('ready,changes,source,overlap,expected', [
    (True, {}, 'web_text', False, '嗯，我在。'),
    (False, {}, 'web_text', False, ''),
    (True, {'recall_memory':True}, 'web_text', False, ''),
    (True, {'level':1}, 'web_text', False, ''),
    (True, {'route':'main'}, 'web_text', False, ''),
    (True, {'reply':'长'*161}, 'web_text', False, ''),
    (True, {}, 'visual_environment', False, ''),
    (True, {}, 'web_text', True, ''),
    (True, {'items':[{'i':1,'person':'P1','certainty':'tentative','why':'还不确定'}]}, 'web_text', False, ''),
])
def test_short_reply_requires_ready_person_and_minimal_route(ready, changes, source, overlap, expected):
    result = VoiceJEV(ShortChatModel(**changes), mode='legacy').decide_batch('stone',
        [{'n':1, 'text':'你好', 'who':'P1', 'source':source}], [],
        {'short_chat':{'allowed':ready}}, overlap=overlap)
    assert result.direct_reply == expected


def test_loaded_materials_are_ephemeral_scoped_and_reused_across_sessions(process):
    state = reply_state(process)
    assert not state.context('stone', ['lux-id'])['allowed']
    result = process.experience('stone', '你好', object_ref='lux', objects={'lux':'lux-id'})
    state.prime('stone', result)
    assert state.context('stone', ['lux-id'])['allowed']
    assert reply_state(process) is state
    assert not state.context('other', ['lux-id'])['allowed']
    assert not state.context('stone', ['lux-id', None])['allowed']
    assert not state.context('stone', ['new-person'])['allowed']
    assert state.accept('stone', 'lux-id', '谢谢', '不用客气。')
    assert state.context('stone', ['lux-id'])['recent'][-1]['reply'] == '不用客气。'
    assert not DirectReplyState().context('stone', ['lux-id'])['allowed']
    failed = replace(result, current_state=replace(result.current_state, source_report=()))
    cold = DirectReplyState()
    cold.prime('stone', failed)
    assert not cold.context('stone', ['lux-id'])['allowed']


@pytest.mark.parametrize('streaming', [False, True])
def test_web_first_turn_main_then_short_reply_skips_main_and_memory(process, streaming):
    model = ShortChatModel()
    async def run():
        async with TestClient(TestServer(create_app(process, 'stone', VoiceConfig(api_key=''), jev=VoiceJEV(model, mode='legacy')))) as client:
            headers = {'Origin':str(client.make_url('')).rstrip('/')}
            first = await client.post('/text-input', headers=headers, json={'text':'你好','object_id':'lux-id'})
            assert first.status == 200, await first.text()
            assert (await first.json())['route'] == 'main'
            # Finish the first turn's deferred writes before taking the snapshot.
            await asyncio.sleep(.15)
            history_before = process.repository.path.read_bytes()
            ledger_before = tuple(process.activity_ledger.list_experiences('stone'))
            count = len(process.cognition.requests)
            root = process.repository.path.parent
            scenes = {p.name:p.read_bytes() for p in root.glob('*scene*.json')}
            if streaming:
                headers['Accept'] = 'application/x-ndjson'
            response = await client.post('/text-input', headers=headers, json={'text':'谢谢','object_id':'lux-id'})
            assert response.status == 200, await response.text()
            if streaming:
                events = [json.loads(row) for row in (await response.text()).splitlines()]
                assert [r['type'] for r in events if r['type'] not in {'flow_phase','model_status'}] == ['accepted','reply','complete']
                assert [r['stage'] for r in events if r['type']=='flow_phase'] == ['jev']
                body = events[-1]
            else:
                body = await response.json()
            assert body['reply'] == '嗯，我在。'
            assert body['route'] == 'self_reply' and body['scene_write'] == 'none'
            entry = body['timing']['end_to_end']
            assert entry['main_process_ms'] == 0 and entry['main_cognition_ms'] == 0
            assert entry['main_called'] is False and entry['memory_written'] is False
            assert entry['reply_ready_ms'] >= entry['jev_ms']
            assert len(process.cognition.requests) == count
            assert process.repository.path.read_bytes() != history_before
            assert any(r.event_type=='jev_direct_reply' for r in process.repository.list_history('stone',limit=100))
            assert tuple(process.activity_ledger.list_experiences('stone')) == ledger_before
            assert {p.name:p.read_bytes() for p in root.glob('*scene*.json')} == scenes
            payloads = [json.loads(r.input_text) for r in model.requests]
            assert payloads[0]['short_chat']['allowed'] is False
            assert payloads[1]['short_chat']['allowed'] is True
            # Important content goes through the normal route chosen by JEV.
            model.changes['route'] = 'main'
            response = await client.post('/text-input', headers={'Origin':headers['Origin']},
                json={'text':'记住我改了名字','object_id':'lux-id'})
            assert response.status == 200, await response.text()
            assert (await response.json())['route'] == 'main'
            assert len(process.cognition.requests) > count
    asyncio.run(run())


@pytest.mark.parametrize('source', ['web_text', 'microphone', 'microphone_live'])
def test_voice_short_reply_and_playback_do_not_write_memories(process, source):
    from time import time
    result = process.experience('stone', '你好', object_ref='lux', objects={'lux':'lux-id'})
    reply_state(process).prime('stone', result)
    count = len(process.cognition.requests)
    ledger = tuple(process.activity_ledger.list_experiences('stone'))
    async def run():
        sent = []
        async def send(message): sent.append(message)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, jev=VoiceJEV(ShortChatModel(), mode='legacy'),
                              scene_idle_s=60, input_pause_s=.01)
        try:
            if source == 'microphone_live':
                c.identities.introduce('A', 'lux', basis='manual_selection')
            before = process.repository.path.read_bytes()
            if source == 'web_text':
                await c.typed('谢谢', 'lux-id')
            elif source == 'microphone_live':
                from jshi.voice.volc import Transcript
                await c.accept(Transcript('谢谢', 'A', 0, 1000, True))
                await asyncio.wait_for(c.queue.join(), 3)
            else:
                speaker = SpeakerEvidence('A', 'lux-id', 'lux', 'recognized', 'voiceprint_match', 1.0)
                u = SceneUtterance('谢谢', speaker, 0, 1000, input_id='audio-short', received_at_ms=time()*1000)
                c.scene.append(u)
                c.enqueue_turn(InputEnvelope(c.session_id, 'microphone', (InputPart('text','谢谢'),), speaker,
                    current_utterances=(u,), deferred_interrupt=True))
            await asyncio.wait_for(c.turn_queue.join(), 3)
            assert any(m['type']=='jev_reply' for m in sent), sent
            reply = next(m for m in sent if m['type']=='reply')
            assert reply['text'] == '嗯，我在。'
            if c.tts_task:
                await asyncio.wait_for(c.tts_task, 3)
            for phase in ('started', 'completed'):
                await c.acknowledge({'reply_id':reply['reply_id'],'segment':0,'phase':phase})
            assert len(process.cognition.requests) == count
            assert tuple(process.activity_ledger.list_experiences('stone')) == ledger
            assert process.repository.path.read_bytes() != before
            records=c.jev_scene_store.snapshot()['events']
            assert any(r['payload'].get('text')=='谢谢' for r in records)
            assert any(r['payload'].get('text')=='嗯，我在。' for r in records)
            assert c.last_debug['timing']['end_to_end']['main_called'] is False
            assert c.last_debug['timing']['end_to_end']['reply_ready_ms'] is not None
        finally:
            await c.close()
    asyncio.run(run())
