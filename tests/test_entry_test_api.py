import asyncio
import io
import wave

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from jshi.app.entry_tests import register
from jshi.voice.recent_audio import RecentAudio


def test_retained_scene_is_independent_and_invalid_ranges_fail(tmp_path):
    async def run():
        recent = RecentAudio(tmp_path/'recent')
        recent.append('capture', 0, b'\0\0'*16000)
        app = web.Application()
        register(app, tmp_path/'library', recent)
        async with TestClient(TestServer(app)) as client:
            r = await client.get('/entry-tests')
            assert r.status == 200 and '试听与截取' in await r.text()
            r = await client.post('/api/entry-tests/retain', json={'capture': 'capture', 'start_ms': 0, 'end_ms': 1000})
            assert r.status == 200
            ident = (await r.json())['media']
            case = {'schema_version': 1, 'name': '人工添加', 'timeline': [{'type': 'audio', 'media': ident, 'at_ms': 0}]}
            r = await client.post('/api/entry-tests/cases', json=case)
            assert r.status == 200
            for path in recent.root.glob('*.pcm'): path.unlink()
            r = await client.get('/api/entry-tests/media/'+ident+'.wav')
            assert r.status == 200
            with wave.open(io.BytesIO(await r.read()), 'rb') as audio:
                assert audio.getnframes() == 16000
            r = await client.post('/api/entry-tests/retain', json={'capture': 'capture', 'start_ms': 1000, 'end_ms': 0})
            assert r.status == 400
            r = await client.post('/api/entry-tests/cases', json={**case, 'timeline': [{'type': 'audio', 'at_ms': 0, 'media': '../escape'}]})
            assert r.status == 400
            r = await client.get('/api/entry-tests/cases')
            assert len(await r.json()) == 1
    asyncio.run(run())
