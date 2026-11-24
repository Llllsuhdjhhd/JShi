"""The device and desktop listeners share one production voice conversation."""
import asyncio
from io import BytesIO
import shutil
import ssl
from types import SimpleNamespace
from time import time

import pytest
from aiohttp import ClientSession, CookieJar, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer, unused_port

from jshi.app.phone_device import PhoneListener
from jshi.app.phone_tls import local_certificate
from jshi.app.voice import create_app
from jshi.vision import VisionConfig, VisionService, VisionStore
from jshi.voice.config import VoiceConfig
from tests.test_voice import FakeCloud, process
from tests.test_voice_transport import Local


@pytest.mark.skipif(not shutil.which('openssl'), reason='OpenSSL is required for the local iPhone CA')
def test_paired_device_audio_camera_reconnect_and_shutdown(process, tmp_path):
    from PIL import Image

    class Vision(VisionService):
        def submit(self, subject, data, captured, source, *, clock):
            frames.append((subject, data, captured, source, clock))
            super().submit(subject,data,captured,source,clock=clock)

    frames = []
    config = VisionConfig(sample_seconds=.5, cloud_enabled=False)
    process.vision = vision = Vision(VisionStore(tmp_path/'vision',config),config)
    args = SimpleNamespace(phone='127.0.0.1', phone_port=unused_port(), port=8765,
        phone_cert=None, phone_key=None, data_dir=tmp_path)
    listener = PhoneListener.from_args(args)
    cert, key, ca = local_certificate(tmp_path/'phone-tls', args.phone)
    assert sorted(p.name for p in ca.parent.iterdir()) == ['iphone-ca.cer', 'server.key', 'server.pem']
    assert not key.stat().st_mode & 0o077
    trust = ssl.create_default_context(cadata=ca.read_bytes())
    root = listener.url.rstrip('/')
    origin = {'Origin':root}

    async def run():
        app = create_app(process,'stone',VoiceConfig(''), local=Local(), local_tts=FakeCloud(),
                         phone_listener=listener)
        async with TestClient(TestServer(app)) as desktop, ClientSession(cookie_jar=CookieJar(unsafe=True)) as phone:
            async def get(path):
                async with phone.get(root+path, ssl=trust) as r:
                    return r.status, await r.text()

            async def pair(code, headers):
                async with phone.post(root+'/pair', data=code, headers=headers, ssl=trust) as r:
                    return r.status

            assert '连接匠石' in (await get('/'))[1]
            assert (await get('/api/debug'))[0] == 404
            with pytest.raises(WSServerHandshakeError) as denied:
                await phone.ws_connect(root+'/voice', headers=origin, ssl=trust)
            assert denied.value.status == 401
            assert await pair(listener.code, {'Origin':'https://other.invalid'}) == 403
            assert await pair('bad', origin) == 400
            wrong_code = f'{(int(listener.code)+1)%100_000_000:08d}'
            assert await pair(wrong_code, origin) == 401
            assert await pair(listener.code, origin) == 200
            data = BytesIO()
            Image.new('RGB',(24,32),'black').save(data,format='JPEG')
            captured = time() - .5
            async def frame(stream, stamp=None):
                async with phone.post(root+'/api/device/frame', data=data.getvalue(), ssl=trust,
                        headers={**origin,'Content-Type':'image/jpeg','X-Device-Lens':'environment',
                                 'X-Device-Stream':stream,'X-Captured-At':str(captured if stamp is None else stamp),'X-Clock-Offset':'0.5'}) as r:
                    return r.status

            local_origin = {'Origin':str(desktop.make_url('/')).rstrip('/')}
            async def desktop_frame(token, source='browser'):
                async with desktop.post('/api/vision/frame', data=data.getvalue(),
                        headers={**local_origin, 'X-Device-Stream':token, 'X-Visual-Source':source}) as response:
                    return response.status

            async def wait_available():
                for _ in range(200):
                    if not (await (await desktop.get('/status')).json())['busy']:
                        return
                    await asyncio.sleep(.01)
                pytest.fail('采集会话没有释放')

            async with desktop.ws_connect('/voice', headers=local_origin) as local_ws:
                ready = await local_ws.receive_json()
                local_token = ready['stream']
                assert await desktop_frame(local_token) == 202
                assert await desktop_frame(ready['capture_id']) == 409
                assert await frame(local_token) == 409
                async with phone.ws_connect(root+'/voice', headers=origin, ssl=trust) as blocked:
                    assert (await blocked.receive_json())['type'] == 'notice'
                # Finish a slow desktop upload only after the other device owns the session.
                started, finish_body = asyncio.Event(), asyncio.Event()
                async def chunks():
                    yield data.getvalue()[:50]
                    started.set()
                    await finish_body.wait()
                    yield data.getvalue()[50:]
                late = asyncio.create_task(desktop.post('/api/vision/frame', data=chunks(),
                    headers={**local_origin, 'X-Device-Stream':local_token, 'X-Visual-Source':'photo'}))
                await asyncio.wait_for(started.wait(), 2)
                await local_ws.send_json({'type':'end'})
            await wait_available()
            await asyncio.get_running_loop().run_in_executor(vision.executor, lambda: None)
            assert vision.store.latest_sample('stone') is None
            frames.clear()

            async with phone.ws_connect(root+'/voice', headers=origin, ssl=trust) as ws:
                ready = await asyncio.wait_for(ws.receive_json(),4)
                assert set(ready) == {'type','stream'} and ready['type'] == 'ready'
                stream = ready['stream']
                finish_body.set()
                async with await asyncio.wait_for(late, 2) as result:
                    assert result.status == 409
                assert (await (await desktop.get('/api/vision')).json())['capture']['kind'] == 'phone'
                assert await desktop_frame(local_token) == 409
                assert await desktop_frame(stream, 'photo') == 409
                async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as other:
                    async with other.post(root+'/pair', data=listener.code, headers=origin, ssl=trust) as paired:
                        assert paired.status == 200
                    async with other.post(root+'/api/device/frame', data=data.getvalue(), ssl=trust,
                            headers={**origin, 'X-Device-Stream':stream, 'X-Device-Lens':'environment',
                                     'Content-Type':'image/jpeg','X-Captured-At':str(time())}) as refused:
                        assert refused.status == 409
                assert await frame('stale') == 409
                assert await frame(stream, time()-60) == 408
                assert await frame(stream) == 202
                assert await frame(stream) == 429
                assert frames[0][:3] == ('stone',data.getvalue(),captured+.5)
                assert frames[0][3].startswith('phone-') and frames[0][3].endswith('-environment')
                assert (await (await desktop.get('/status')).json())['busy']
                with pytest.raises(WSServerHandshakeError) as busy:
                    await desktop.ws_connect('/voice', headers={'Origin':str(desktop.make_url('/')).rstrip('/')})
                assert busy.value.status == 409
                await ws.send_json({'type':'text','text':'忽略设备管理操作'})
                await ws.send_bytes(bytes(640))
                events = []
                while not any(e['type']=='audio_end' for e in events):
                    events.append(await asyncio.wait_for(ws.receive_json(),5))
                assert any(e['type']=='audio' and e['pcm'] for e in events)
                rid = next(e['reply_id'] for e in events if e['type']=='reply')
                for phase in ('started','completed'):
                    await ws.send_json({'type':'played','reply_id':rid,'segment':0,'phase':phase})
                await ws.send_json({'type':'stop'})
                while (await asyncio.wait_for(ws.receive_json(),3))['type'] != 'stop':
                    pass
                await ws.send_json({'type':'end'})
            await wait_available()
            assert await frame(stream) == 409
            async with desktop.ws_connect('/voice', headers=local_origin) as local_ws:
                token = (await local_ws.receive_json())['stream']
                assert token != local_token
                assert await desktop_frame(local_token) == 409
                assert await frame(stream) == 409
                assert await desktop_frame(token, 'photo') == 202
                await local_ws.send_json({'type':'end'})
            await wait_available()
            async with phone.ws_connect(root+'/voice', headers=origin, ssl=trust) as again:
                assert (await again.receive_json())['stream'] != stream
                assert await frame(stream) == 409
                # Host shutdown must release an active device, including its socket and camera worker.
                await asyncio.wait_for(desktop.close(), 8)
                closed = await asyncio.wait_for(again.receive(), 2)
                assert closed.type.name in {'CLOSE','CLOSED'}
        assert vision.closed
        with pytest.raises(OSError):
            await asyncio.open_connection('127.0.0.1',args.phone_port)
    asyncio.run(run())


def test_new_source_uses_the_same_exclusive_capture_slot():
    from jshi.app.capture import CaptureSource, CaptureSources, DESKTOP

    inputs = CaptureSources()
    cameras = [CaptureSource('network_camera', 'room-a'), CaptureSource('network_camera', 'room-b')]
    first = inputs.acquire(cameras[0], 'first')
    assert inputs.resolve(first.source, first.token) is None
    first.accepting = True
    for other in [DESKTOP, *cameras]:
        with pytest.raises(RuntimeError):
            inputs.acquire(other, 'overlap')
    assert inputs.resolve(cameras[1], first.token) is None
    assert inputs.resolve(cameras[0], first.token) is first
    inputs.release(first)
    second = inputs.acquire(cameras[0], 'second')
    second.accepting = True
    inputs.release(first)
    assert inputs.resolve(cameras[0], first.token) is None
    assert inputs.resolve(cameras[0], second.token) is second
