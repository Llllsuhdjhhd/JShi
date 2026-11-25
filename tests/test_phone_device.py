"""The device and desktop listeners share one production voice conversation."""
import asyncio
from io import BytesIO
import os
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


def test_paired_device_audio_camera_reconnect_and_shutdown(process, tmp_path):
    from PIL import Image

    class Vision(VisionService):
        def submit(self, subject, data, captured, source, *, clock):
            frames.append((subject, data, captured, source, clock))
            super().submit(subject,data,captured,source,clock=clock)

    class Microphone(Local):
        def feed(self, pcm, final=False):
            if pcm: recordings.append(bytes(pcm))
            return super().feed(pcm, final)

    recordings = []
    frames = []
    config = VisionConfig(sample_seconds=.5, cloud_enabled=False)
    process.vision = vision = Vision(VisionStore(tmp_path/'vision',config),config)
    args = SimpleNamespace(phone='127.0.0.1', phone_port=unused_port(), port=8765,
        phone_cert=None, phone_key=None, data_dir=tmp_path)
    listener = PhoneListener.from_args(args)
    cert, key, ca = local_certificate(tmp_path/'phone-tls', args.phone)
    assert sorted(p.name for p in ca.parent.iterdir()) == ['iphone-ca.cer', 'server.key', 'server.pem']
    if os.name != "nt":  # Windows protects files using ACLs.
        assert not key.stat().st_mode & 0o077
    trust = ssl.create_default_context(cadata=ca.read_bytes())
    root = listener.url.rstrip('/')
    origin = {'Origin':root}

    async def run():
        app = create_app(process,'stone',VoiceConfig(''), local=Microphone(), local_tts=FakeCloud(),
                         phone_listener=listener)
        async with TestClient(TestServer(app)) as desktop, ClientSession(cookie_jar=CookieJar(unsafe=True)) as phone:
            async def get(path):
                async with phone.get(root+path, ssl=trust) as r:
                    return r.status, await r.text()

            async def pair(code, headers):
                async with phone.post(root+'/pair', data=code, headers=headers, ssl=trust) as r:
                    return r.status

            assert '启用手机设备' in (await get('/'))[1]
            assert (await get('/api/debug'))[0] == 404
            with pytest.raises(WSServerHandshakeError) as denied:
                await phone.ws_connect(root+'/device-link', headers=origin, ssl=trust)
            assert denied.value.status == 401
            assert await pair(listener.code, {'Origin':'https://other.invalid'}) == 403
            assert await pair('bad', origin) == 400
            wrong_code = f'{(int(listener.code)+1)%100_000_000:08d}'
            assert await pair(wrong_code, origin) == 401
            assert await pair(listener.code, origin) == 200
            data = BytesIO()
            Image.new('RGB',(24,32),'black').save(data,format='JPEG')
            captured = time() - .5
            camera_token = ""
            async def frame(stream, stamp=None):
                async with phone.post(root+'/api/device/frame', data=data.getvalue(), ssl=trust,
                        headers={**origin,'Content-Type':'image/jpeg','X-Device-Lens':'environment',
                                 'X-Device-Stream':stream,'X-Device-Camera':camera_token,'X-Captured-At':str(captured if stamp is None else stamp),'X-Clock-Offset':'0.5'}) as r:
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

            async with phone.ws_connect(root+'/device-link?playback=1', headers=origin, ssl=trust) as sensor:
                available = await sensor.receive_json()
                device_id = available['device_id']
                devices = await (await desktop.get('/api/capture-sources')).json()
                assert devices['phones'] == [device_id]
                assert not (await (await desktop.get('/status')).json())['busy']
                async with desktop.ws_connect('/voice', headers=local_origin) as local_ws:
                    local_token = (await local_ws.receive_json())['stream']
                    assert await desktop_frame(local_token) == 202
                    assert await frame(local_token) == 409
                    await local_ws.send_json({'type':'end'})
                await wait_available()
                frames.clear()

                async with desktop.ws_connect('/voice?source='+device_id, headers=local_origin) as ws:
                    ready = await ws.receive_json()
                    assert ready['type']=='ready' and 'asr' in ready
                    stream = (await sensor.receive_json())['stream']
                    assert stream != ready['stream']
                    assert await frame(stream) == 409
                    assert await desktop_frame(local_token) == 409
                    assert await desktop_frame(ready['stream']) == 409
                    await ws.send_json({'type':'device_camera','enabled':True})
                    camera_token = (await sensor.receive_json())['camera']
                    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as other:
                        async with other.post(root+'/pair', data=listener.code, headers=origin, ssl=trust) as paired:
                            assert paired.status == 200
                        async with other.post(root+'/api/device/frame', data=data.getvalue(), ssl=trust,
                                headers={**origin,'X-Device-Stream':stream, 'X-Device-Camera':camera_token,
                                         'X-Device-Lens':'environment','Content-Type':'image/jpeg',
                                         'X-Captured-At':str(time())}) as refused:
                            assert refused.status == 409
                    assert await frame('stale') == 409
                    assert await frame(stream, time()-60) == 408
                    assert await frame(stream) == 202
                    assert await frame(stream) == 429
                    assert frames[0][:3] == ('stone',data.getvalue(),captured+.5)
                    assert frames[0][3] == 'phone-'+device_id+'-environment'
                    await ws.send_json({'type':'device_camera','enabled':False})
                    assert not (await sensor.receive_json())['camera']
                    assert await frame(stream) == 409
                    assert await desktop_frame(ready['stream'], 'photo') == 202

                    await ws.send_bytes(bytes([1])*640)
                    await ws.send_bytes(b'JREF' + bytes([7])*640)
                    await sensor.send_bytes(bytes([2])*640)
                    await sensor.send_json({'type':'started','stream':stream,'echo_reference':True})
                    await sensor.send_bytes(b'JREF'+bytes([7])*640)
                    await sensor.send_bytes(bytes(640))
                    events = []
                    while not any(e['type']=='audio_end' for e in events):
                        events.append(await asyncio.wait_for(ws.receive_json(),5))
                    assert not any(e['type']=='audio' for e in events)
                    playback = []
                    while not playback or playback[-1]['type'] != 'audio_end':
                        packet = await asyncio.wait_for(sensor.receive_json(), 5)
                        assert packet['stream'] == stream and packet['type'] == 'playback'
                        playback.append(packet['event'])
                    assert any(e['type']=='audio' and e['pcm'] for e in playback)
                    assert all('text' not in e and 'items' not in e for e in playback)
                    assert recordings == [bytes(640)]
                    rid = next(e['reply_id'] for e in events if e['type']=='reply')
                    await ws.send_json({'type':'played','reply_id':rid,'segment':0,'phase':'completed'})
                    for phase in ('started','completed'):
                        await sensor.send_json({'type':'played','stream':stream,'reply_id':rid,'segment':0,'phase':phase})
                        while True:
                            receipt = await asyncio.wait_for(ws.receive_json(), 5)
                            if receipt['type']=='phone_played': break
                        assert receipt['reply_id']==rid and receipt['phase']==phase
                    await ws.send_json({'type':'end'})
                assert (await sensor.receive_json())['type']=='standby'
                await wait_available()
                assert await frame(stream) == 409
                async with desktop.ws_connect('/voice?source='+device_id, headers=local_origin) as ws:
                    assert (await ws.receive_json())['type']=='ready'
                    assert (await sensor.receive_json())['stream'] != stream
                    await sensor.close()
                    while not ws.closed:
                        event=await asyncio.wait_for(ws.receive(),3)
                        if event.type.name in ('CLOSE','CLOSED'): break
                await wait_available()
                assert (await (await desktop.get('/api/capture-sources')).json())['phones'] == []
                with pytest.raises(WSServerHandshakeError) as offline:
                    await desktop.ws_connect('/voice?source='+device_id, headers=local_origin)
                assert offline.value.status == 409
            await desktop.close()
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


def test_phone_echo_preserves_near_speech_and_removes_delayed_phone_playback():
    np = pytest.importorskip('numpy')
    from jshi.voice.playback_echo import PlaybackEcho

    rng = np.random.default_rng(15)
    rate = 16_000
    total = rate * 7
    def speech(length):
        return np.convolve(rng.normal(0, 4400, length), np.ones(3) / 3, 'same').astype('<i2')
    reference = speech(total)
    reflected = .55 * np.roll(reference, 830) + .16 * np.roll(reference, 1050)
    near = np.zeros(total)
    near[64000:80000] = speech(rate)
    mic = (near + reflected).astype('<i2')
    echo = PlaybackEcho()
    assert echo.clean(mic[:320].tobytes()) == mic[:320].tobytes()
    cleaned = bytearray()
    for i in range(0, total, 320):
        echo.reference(reference[i:i+320].tobytes())
        cleaned.extend(echo.clean(mic[i:i+320].tobytes()))
    output = np.frombuffer(cleaned, '<i2').astype(float)
    def rms(x):
        return np.sqrt(np.mean(x*x))
    assert rms(output[3200:14400]) < .2 * rms(reflected[3200:14400])
    assert rms(output[40000:62000]) < .18 * rms(reflected[40000:62000])
    # With a second speaker present, their utterance remains correlated, even with DSP frame delay.
    near_only = near[66000:76000]
    candidate = output[66000:76800]
    peak = np.max(np.correlate(candidate, near_only, 'valid')) / np.dot(near_only, near_only)
    assert peak > .5


def test_native_aec_already_removed_reply_preserves_unrelated_speech():
    import numpy as np
    from jshi.voice.playback_echo import PlaybackEcho

    rng = np.random.default_rng(384)
    echo = PlaybackEcho()
    for _ in range(70):
        far = rng.normal(0, 2200, 320).astype('<i2').tobytes()
        near = rng.normal(0, 1200, 320).astype('<i2').tobytes()
        echo.reference(far)
        assert echo.clean(near) == near
