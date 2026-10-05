import asyncio
import json
import struct
from types import SimpleNamespace

import pytest

from jshi.voice.tts_ws import pack_tts, unpack_tts
from jshi.voice.volc import VolcVoice
from jshi.voice.config import VoiceConfig


def server_frame(event, sid, payload=b'{}', audio=False):
    name = sid.encode()
    return bytes((0x11, 0xb4 if audio else 0x94, 0 if audio else 0x10, 0)) + struct.pack(
        '>iI', event, len(name)) + name + struct.pack('>I', len(payload)) + payload


def test_tts_frames_validate_ids_and_errors():
    frame = server_frame(352, 'session', b'\0\0', True)
    assert unpack_tts(frame) == (11, 352, 'session', b'\0\0')
    with pytest.raises(ValueError):
        unpack_tts(frame[:-1])
    error = bytes((0x11, 0xf0, 0x10, 0)) + struct.pack('>II', 45000000, 6) + b'denied'
    with pytest.raises(RuntimeError, match='45000000'):
        unpack_tts(error)
    assert pack_tts(1) == bytes((0x11,0x14,0x10,0)) + struct.pack('>iI',1,2) + b'{}'


class WS:
    def __init__(self):
        self.closed = False
        self.queue = asyncio.Queue()
        self.events = []
    async def send_bytes(self, frame):
        event = struct.unpack_from('>i',frame,4)[0]
        self.events.append(event)
        sid = ''
        if event not in (1,2):
            size = struct.unpack_from('>I',frame,8)[0]
            sid = frame[12:12+size].decode()
        if event == 1:
            await self.queue.put(server_frame(50,'connection'))
        elif event == 100:
            await self.queue.put(server_frame(150,sid))
        elif event == 200:
            await self.queue.put(server_frame(352,sid,b'\0\0'*100,True))
        elif event == 102:
            await self.queue.put(server_frame(152,sid))
    async def receive(self):
        import aiohttp
        return SimpleNamespace(type=aiohttp.WSMsgType.BINARY,data=await self.queue.get())
    async def close(self): self.closed=True


class HTTP:
    def __init__(self): self.connections=[]
    async def ws_connect(self,url,**kwargs):
        ws = WS()
        self.connections.append(ws)
        return ws


def test_successful_tts_sessions_reuse_connection_and_cancel_discards_it():
    async def run():
        http=HTTP();cloud=VolcVoice(VoiceConfig('secret'),http)
        assert b''.join([x async for x in cloud.synthesize('第一句。')])
        assert b''.join([x async for x in cloud.synthesize('第二句。')])
        assert len(http.connections)==1
        generator=cloud.synthesize('这是要停止的句子。')
        await anext(generator)
        await generator.aclose()
        assert 101 in http.connections[0].events and http.connections[0].closed
        assert b''.join([x async for x in cloud.synthesize('新的回应。')])
        assert len(http.connections)==2
        await cloud.close()
    asyncio.run(run())


def test_idle_tts_connection_is_replaced_before_next_text(monkeypatch):
    now = [10.]
    monkeypatch.setattr('jshi.voice.tts_ws.monotonic',lambda:now[0])
    async def run():
        http=HTTP();cloud=VolcVoice(VoiceConfig('secret'),http)
        assert [p async for p in cloud.synthesize('第一句。')]
        now[0] += 9
        assert [p async for p in cloud.synthesize('第二句。')]
        assert len(http.connections)==2 and http.connections[0].closed
        assert http.connections[0].events.count(200)==1
        await cloud.close()
    asyncio.run(run())


def test_stale_transport_retry_happens_before_text():
    async def run():
        import aiohttp
        http=HTTP();cloud=VolcVoice(VoiceConfig('secret'),http)
        assert [p async for p in cloud.synthesize('第一句。')]
        old=http.connections[0];original=old.send_bytes
        async def broken(frame):
            if struct.unpack_from('>i',frame,4)[0]==100:
                raise aiohttp.ClientConnectionError('Cannot write to closing transport')
            await original(frame)
        old.send_bytes=broken
        assert [p async for p in cloud.synthesize('第二句。')]
        assert old.events.count(200)==1
        assert len(http.connections)==2
        await cloud.close()
    asyncio.run(run())
