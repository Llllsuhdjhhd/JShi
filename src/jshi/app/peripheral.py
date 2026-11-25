"""A connected peripheral supplies media to a desktop-owned conversation."""
import asyncio
import json
from contextlib import suppress
from secrets import token_urlsafe

from aiohttp import WSMessage, WSMsgType

from .capture import CaptureSource
from jshi.voice.playback_echo import PlaybackEcho


class Peripheral:
    def __init__(self, source: CaptureSource, socket, *, playback=False):
        self.source = source
        self.socket = socket
        self.session = None
        self.controller = None
        self.queue = None
        self.pump = None
        self.audio_ready = False
        self.camera_token = ''
        self.echo = None
        self.playback = playback
        self.stopping = False

    async def attach(self, session, controller):
        if self.socket.closed or self.session is not None:
            raise RuntimeError('手机来源暂不可用，请重新选择')
        self.session, self.controller = session, controller
        self.stopping = False
        self.queue = asyncio.Queue(maxsize=12)
        self.pump = asyncio.create_task(self.read_controller())
        await self.socket.send_json({'type':'start', 'stream':session.token})

    async def deliver(self, event, controller):
        """Keep the transcript on the controller and render audio only on its bound phone."""
        kind = event['type']
        if kind in {'reply','audio','segment_end','audio_end','pause','resume','stop','block_next','clear_block'}:
            if self.stopping or self.session is None or self.controller is not controller or not self.session.accepting:
                return
            payload = {k:event[k] for k in ('type','reply_id','segment','rate','pcm','paused','permit_seq','reason_id','cleared')
                       if k in event}
            await self.socket.send_json({'type':'playback','stream':self.session.token,'event':payload})
            if kind == 'audio':
                return
        await controller.send_json(event)

    async def read_controller(self):
        try:
            async for message in self.controller:
                # PCM and microphone activity may only come from the selected peripheral.
                if message.type == WSMsgType.BINARY:
                    continue
                if message.type == WSMsgType.TEXT:
                    kind = message.json().get('type')
                    if kind in {'speech_start','speech_end','audio_clock','played'}:
                        continue
                    if kind == 'end':
                        await self.standby()
                await self.queue.put(message)
        finally:
            if self.session is not None:
                await self.standby()
                await self.queue.put(None)

    async def standby(self):
        if self.stopping:
            return
        self.stopping = True
        self.audio_ready = False
        if not self.socket.closed:
            with suppress(ConnectionError):
                await self.socket.send_json({'type':'standby'})

    async def messages(self):
        while (message := await self.queue.get()) is not None:
            yield message

    async def camera(self, enabled):
        self.camera_token = token_urlsafe(24) if enabled else ''
        await self.socket.send_json({'type':'camera', 'stream':self.session.token,
                                     'camera':self.camera_token})

    async def incoming(self, message):
        session = self.session
        if self.stopping or not session or not session.accepting:
            return
        if message.type == WSMsgType.TEXT:
            event = message.json()
            if event.get('stream') != session.token:
                return
            kind = event.get('type')
            if kind == 'started':
                if not self.audio_ready and event.get('echo_reference') is True:
                    self.echo = PlaybackEcho()
                self.audio_ready = True
                return
            if kind == 'played':
                if (not self.audio_ready or not isinstance(event.get('reply_id'), str)
                        or type(event.get('segment')) is not int or event['segment'] < 0
                        or event.get('phase') not in {'started','completed'}):
                    return
                await self.controller.send_json({k:event[k] for k in ('reply_id','segment','phase','browser_at_ms')
                                                 if k in event} | {'type':'phone_played'})
                await self.queue.put(message)
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
            if self.echo and kind in {'speech_start','speech_end'}:
                return
        elif message.type != WSMsgType.BINARY:
            return
        if not self.audio_ready:
            return
        if message.type == WSMsgType.BINARY and len(message.data) != 640:
            if self.echo and len(message.data) == 644 and message.data.startswith(PlaybackEcho.tag):
                self.echo.reference(message.data[4:])
            return
        activity = None
        if self.echo and message.type == WSMsgType.BINARY and len(message.data) == 640:
            pcm = self.echo.clean(message.data)
            activity = self.echo.activity(pcm)
            message = WSMessage(WSMsgType.BINARY, pcm, '')
        if self.queue.qsize() + 1 + bool(activity) > self.queue.maxsize:
            await self.lost('手机声音传输积压，本次采集已停止')
        else:
            if activity:
                self.queue.put_nowait(WSMessage(WSMsgType.TEXT, json.dumps(activity), ''))
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
        self.echo = None
        self.camera_token = ''
        self.pump.cancel()
        await asyncio.gather(self.pump, return_exceptions=True)
        self.controller = self.queue = self.pump = None
        await self.standby()
