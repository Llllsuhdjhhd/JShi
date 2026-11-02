import asyncio
from aiohttp.test_utils import TestClient, TestServer
from jshi.app.voice import create_app
from jshi.voice.config import VoiceConfig
from tests.test_voice import process, FakeCloud


def test_voice_model_status_and_unavailable_tts_choice(process):
    async def run():
        app = create_app(process, 'stone', VoiceConfig('', asr_backend='local', tts_backend='local'), local_tts=FakeCloud())
        async with TestClient(TestServer(app)) as client:
            result = await (await client.get('/status')).json()
            assert result['tts_backends'] == {'local':True, 'online':False}
            assert result['tts_backend'] == 'local'
            assert 'bailian' in result['asr_backends']
            origin = str(client.make_url('/')).rstrip('/')
            response = await client.get('/voice?tts=online', headers={'Origin':origin})
            assert response.status == 400 and '火山播报缺少' in await response.text()
            response = await client.get('/voice?tts=invalid', headers={'Origin':origin})
            assert response.status == 400 and '未知语音播报' in await response.text()
    asyncio.run(run())
