import asyncio
from aiohttp.test_utils import TestClient,TestServer
from jshi.app.voice import create_app
from jshi.voice.config import VoiceConfig
from tests.test_voice import process,FakeCloud


def test_association_api_keeps_subject_scope_and_same_origin(process):
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig('',asr_backend='local',tts_backend='local'),local_tts=FakeCloud()))) as client:
            response=await client.get('/person-links')
            assert response.status==200 and (await response.json())['links']==[]
            response=await client.post('/person-links',json={'action':'revoke','link_id':'unknown'})
            assert response.status==403
            response=await client.post('/person-links',headers={'Origin':str(client.make_url('/')).rstrip('/')},json={'action':'revoke','link_id':'unknown'})
            assert response.status==400
    asyncio.run(run())
