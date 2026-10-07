import asyncio
import json
from threading import Event
from types import SimpleNamespace

import pytest

from jshi.app.voice import VoiceConversation, create_app
from jshi.models import ModelResponse
from jshi.voice.config import VoiceConfig
from jshi.voice.jev import VoiceJEV
from tests.test_voice import FakeCloud, process


@pytest.mark.parametrize('routing', [{}, {'level': 9, 'recall_memory': 'false'}])
def test_missing_or_invalid_routing_does_not_discard_valid_identity(routing):
    class Judge:
        def generate(self, request):
            return ModelResponse(model='test', text=json.dumps({
                'items': [{'i': 1, 'person': 'P1', 'certainty': 'confirmed', 'why': '声纹明确匹配P1'}],
                **routing}))
    result = VoiceJEV(Judge()).decide_batch('stone', [{
        'n': 1, 'who': 'P1（lux）', 'text': '你好',
        'voice_evidence': {'pick': 'P1', 'strength': 'clear'}}], [], {})
    assert result.judged and not result.model_error
    assert result.items[0].speaker_pick == 'P1' and result.items[0].speaker_level == '确定'
    assert result.main_prompt_hint == {'level': 1, 'needs': [], 'recall_memory': False}
    assert result.routing_error


def test_invalid_identity_still_fails_even_when_routing_missing():
    class Judge:
        def generate(self, request):
            return ModelResponse(model='test', text=json.dumps({'items': [
                {'i': 1, 'person': 'P999', 'certainty': 'confirmed', 'why': '错误人选'}]}))
    result = VoiceJEV(Judge()).decide_batch('stone', [{'n': 1, 'text': '你好'}], [], {})
    assert not result.judged and result.model_error


def test_close_does_not_wait_for_discarded_jev_http_request(process):
    entered, release = Event(), Event()
    def blocked():
        entered.set()
        release.wait(3)
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send)
        future = asyncio.get_running_loop().run_in_executor(c.jev_executor, blocked)
        c.jev_future = future
        try:
            while not entered.is_set(): await asyncio.sleep(.001)
            stages = []
            await asyncio.wait_for(c.close(on_progress=stages.append), .5)
            assert not future.done()
            assert c.closed and '等待已启动的主认知结束并保存' in stages
        finally:
            release.set()
            await future
            await c.close()
    asyncio.run(run())


def test_close_still_waits_for_active_cognition_to_finish_saving(process):
    entered, release = Event(), Event()
    def saving():
        entered.set()
        release.wait(3)
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send)
        future = asyncio.get_running_loop().run_in_executor(c.turn_executor, saving)
        task = None
        try:
            while not entered.is_set(): await asyncio.sleep(.001)
            stages = []
            task = asyncio.create_task(c.close(on_progress=stages.append))
            while '等待已启动的主认知结束并保存' not in stages: await asyncio.sleep(.001)
            assert not task.done() and not future.done()
            release.set()
            await asyncio.wait_for(task, 1)
            assert future.done()
        finally:
            release.set()
            if task: await task
            await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['error', 'eof', 'cleanup_error'])
def test_asr_disconnect_reports_failure_and_allows_new_connection(process, monkeypatch, mode):
    aiohttp = pytest.importorskip('aiohttp')
    from aiohttp.test_utils import TestClient, TestServer
    class ASR:
        close_code = 1006
        def __init__(self): self.queue = asyncio.Queue()
        def exception(self): return RuntimeError('upstream reset')
        def __aiter__(self): return self
        async def __anext__(self):
            message = await self.queue.get()
            if message is None: raise StopAsyncIteration
            return message
        async def send_bytes(self, frame):
            await self.queue.put(None if mode == 'eof' else SimpleNamespace(type=aiohttp.WSMsgType.ERROR))
        async def close(self):
            if mode == 'cleanup_error': raise RuntimeError('ASR cleanup failed')
    class Cloud(FakeCloud):
        async def open_asr(self): return ASR()
    monkeypatch.setattr('jshi.app.voice.VolcVoice', lambda config, http: Cloud())
    async def run():
        async with TestClient(TestServer(create_app(process, 'stone', VoiceConfig('secret')))) as client:
            origin = str(client.make_url('/')).rstrip('/')
            ws = await client.ws_connect('/voice?asr=online', headers={'Origin': origin})
            assert (await ws.receive_json())['type'] == 'ready'
            await ws.send_bytes(b'\0\0' * 1600)
            notice = await asyncio.wait_for(ws.receive_json(), 2)
            assert notice['type'] == 'notice' and 'code=1006' in notice['text']
            assert 'upstream reset' in notice['text']
            await asyncio.wait_for(ws.receive(), 2)
            assert ws.close_code == 1011
            async def settled():
                while True:
                    status = await (await client.get('/status')).json()
                    if not status['busy']:
                        assert status['cleanup_stage'] == ''
                        return
                    await asyncio.sleep(.005)
            await asyncio.wait_for(settled(), 2)
            failures = [r for r in process.repository.list_history('stone', limit=100)
                        if r.event_type == 'voice_asr_failure']
            assert failures and failures[-1].content['close_code'] == 1006
            other = await client.ws_connect('/voice?asr=online', headers={'Origin': origin})
            assert (await other.receive_json())['type'] == 'ready'
            await other.close()
    asyncio.run(run())
