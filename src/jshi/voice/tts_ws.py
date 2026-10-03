"""Volcengine V3 bidirectional TTS: streamed text, streamed PCM, session cancel."""
from __future__ import annotations

import asyncio
import gzip
import json
import struct
from time import monotonic
from uuid import uuid4


def pack_tts(event: int, payload=None, session_id: str = '') -> bytes:
    body = json.dumps(payload or {}, ensure_ascii=False).encode()
    frame = bytes((0x11, 0x14, 0x10, 0)) + struct.pack('>i', event)
    if event not in (1, 2):
        sid = session_id.encode()
        frame += struct.pack('>I', len(sid)) + sid
    return frame + struct.pack('>I', len(body)) + body


def unpack_tts(frame: bytes) -> tuple[int, int, str, bytes]:
    if len(frame) < 8 or frame[0] >> 4 != 1:
        raise ValueError('invalid TTS frame')
    offset = (frame[0] & 15) * 4
    kind, flags = frame[1] >> 4, frame[1] & 15
    def integer():
        nonlocal offset
        if offset + 4 > len(frame):
            raise ValueError('truncated TTS field')
        value = struct.unpack_from('>I', frame, offset)[0]
        offset += 4
        return value
    def block():
        nonlocal offset
        size = integer()
        if offset + size > len(frame):
            raise ValueError('truncated TTS payload')
        value = frame[offset:offset+size]
        offset += size
        return value
    code = integer() if kind == 15 else 0
    if kind != 15 and flags & 1:
        integer()  # optional sequence
    event = integer() if flags & 4 else 0
    sid = block().decode() if event else ''  # connection events carry connect id
    payload = block()
    if offset != len(frame):
        raise ValueError('unexpected TTS trailing bytes')
    if frame[2] & 15 == 1:
        payload = gzip.decompress(payload)
    if kind == 15:
        raise RuntimeError(f'TTS error {code}: {payload.decode("utf-8", "replace")}')
    return kind, event, sid, payload


class BidirectionalTTS:
    def __init__(self, http, headers, request):
        self.http, self.headers, self.request = http, headers, request
        self.ws = None
        self.lock = asyncio.Lock()
        self.last_used = 0.0

    async def receive(self):
        import aiohttp
        msg = await asyncio.wait_for(self.ws.receive(), 20)
        if msg.type != aiohttp.WSMsgType.BINARY:
            raise RuntimeError('TTS connection closed before session finished')
        result = unpack_tts(msg.data)
        if result[1] in (51, 153):
            raise RuntimeError('TTS session failed: ' + result[3].decode('utf-8', 'replace'))
        return result

    async def connect(self):
        # Cloud idle timeouts can precede aiohttp's local closed flag.
        if self.ws is not None and monotonic() - self.last_used > 8:
            await self.close()
        if self.ws is not None and not self.ws.closed:
            return
        headers = self.headers()
        headers['X-Api-Connect-Id'] = str(uuid4())
        self.ws = await self.http.ws_connect('wss://openspeech.bytedance.com/api/v3/tts/bidirection',
            headers=headers, heartbeat=20, max_msg_size=4*1024*1024)
        await self.ws.send_bytes(pack_tts(1))
        if (await self.receive())[1] != 50:
            raise RuntimeError('TTS connection not acknowledged')

    async def stream(self, texts):
        async with self.lock:
            sender = None
            sid = uuid4().hex
            finished = False
            try:
                request = self.request('')
                request['req_params'].pop('text', None)
                request.update(event=100, namespace='BidirectionalTTS')
                reused = self.ws is not None and not self.ws.closed
                import aiohttp
                for attempt in range(2):
                    try:
                        await self.connect()
                        await self.ws.send_bytes(pack_tts(100, request, sid))
                        response = await self.receive()
                        break
                    except (aiohttp.ClientConnectionError, ConnectionError, asyncio.TimeoutError):
                        if not reused or attempt:
                            raise
                        await self.close()
                    except RuntimeError as exc:
                        if not reused or attempt or 'connection closed' not in str(exc):
                            raise
                        await self.close()
                # Retry is only before sending text. Never replay audio/text
                # after synthesis has started.
                if response[1] != 150 or response[2] != sid:
                    raise RuntimeError('TTS session not acknowledged')
                async def send_texts():
                    async for text in texts:
                        if not text:
                            continue
                        body = self.request(text)
                        body.update(event=200, namespace='BidirectionalTTS')
                        await self.ws.send_bytes(pack_tts(200, body, sid))
                    await self.ws.send_bytes(pack_tts(102, session_id=sid))
                sender = asyncio.create_task(send_texts())
                got_audio = False
                while True:
                    if sender.done():
                        await sender
                    kind, event, response_sid, payload = await self.receive()
                    if response_sid != sid:
                        raise RuntimeError('TTS session id mismatch')
                    if kind == 11 and event == 352 and payload:
                        got_audio = True
                        yield payload
                    elif event == 152:
                        await sender
                        if not got_audio:
                            raise RuntimeError('TTS returned no audio')
                        finished = True
                        self.last_used = monotonic()
                        break
            finally:
                if sender is not None:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                if not finished:
                    # Stop both further text and server-side generation. Close
                    # rather than reuse a connection with unread canceled audio.
                    if self.ws is not None and not self.ws.closed:
                        try:
                            await asyncio.wait_for(self.ws.send_bytes(pack_tts(101, session_id=sid)), .5)
                        except Exception:
                            pass
                    await self.close()

    async def close(self):
        ws, self.ws = self.ws, None
        if ws is not None and not ws.closed:
            await ws.close()
