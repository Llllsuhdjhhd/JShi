"""A connected peripheral supplies media to a desktop-owned conversation."""
import asyncio
from contextlib import suppress
from secrets import token_urlsafe

from aiohttp import WSMsgType

from .capture import CaptureSource


class Peripheral:
    def __init__(self, source: CaptureSource, socket):
        self.source = source
        self.socket = socket
        self.session = None
        self.controller = None
        self.queue = None
        self.pump = None
        self.audio_ready = False
        self.camera_token = ''

    async def attach(self, session, controller):
        if self.socket.closed or self.session is not None:
            raise RuntimeError('手机来源暂不可用，请重新选择')
        self.session, self.controller = session, controller
        self.queue = asyncio.Queue(maxsize=12)
        self.pump = asyncio.create_task(self.read_controller())
        await self.socket.send_json({'type':'start', 'stream':session.token})

    async def read_controller(self):
        try:
            async for message in self.controller:
                # PCM and microphone activity may only come from the selected peripheral.
                if message.type == WSMsgType.BINARY:
                    continue
                if message.type == WSMsgType.TEXT and message.json().get('type') in (
                        'speech_start','speech_end','audio_clock'):
                    continue
                await self.queue.put(message)
        finally:
            if self.session is not None:
                await self.queue.put(None)

    async def messages(self):
        while (message := await self.queue.get()) is not None:
            yield message

    async def camera(self, enabled):
        self.camera_token = token_urlsafe(24) if enabled else ''
        await self.socket.send_json({'type':'camera', 'stream':self.session.token,
                                     'camera':self.camera_token})

    async def incoming(self, message):
        session = self.session
        if not session or not session.accepting:
            return
        if message.type == WSMsgType.TEXT:
            event = message.json()
            if event.get('stream') != session.token:
                return
            kind = event.get('type')
            if kind == 'started':
                self.audio_ready = True
                return
            if kind == 'preview_signal':
                if self.camera_token and event.get('camera') == self.camera_token:
                    await self.controller.send_json({'type':'preview_signal','camera':self.camera_token,
                        'id':event.get('id'),'sdp':event.get('sdp')})
                return
            if kind == 'device_video':
                if self.camera_token and event.get('camera') == self.camera_token:
                    await self.controller.send_json({'type':kind,'text':str(event.get('text',''))[:80],
                                                     'sending':event.get('sending') is True})
                return
            if kind == 'device_status':
                await self.controller.send_json({'type':'device_status', 'text':str(event.get('text',''))[:200],
                                                 'muted':event.get('muted') is True})
                return
            if kind not in {'speech_start','speech_end','audio_clock'}:
                return
        elif message.type != WSMsgType.BINARY:
            return
        if not self.audio_ready:
            return
        if self.queue.full():
            await self.lost('手机声音传输积压，本次采集已停止')
        else:
            self.queue.put_nowait(message)

    async def lost(self, reason='手机来源已断开，本次采集已停止'):
        if self.session:
            self.session.accepting = False
            if not self.controller.closed:
                try:
                    await self.controller.send_json({'type':'notice','text':reason})
                finally:
                    await self.controller.close(code=1001, message=reason.encode())

    async def detach(self, session):
        if self.session is not session:
            return
        self.session = None
        self.audio_ready = False
        self.camera_token = ''
        self.pump.cancel()
        await asyncio.gather(self.pump, return_exceptions=True)
        self.controller = self.queue = self.pump = None
        if not self.socket.closed:
            with suppress(ConnectionError):
                await self.socket.send_json({'type':'standby'})
