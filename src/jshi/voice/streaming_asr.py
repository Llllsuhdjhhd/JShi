"""Cloud streaming protocol adapters exposing one session interface to the app."""
from .volc import pack_asr, unpack_asr
from .transcripts import TranscriptAssembler


class VolcProtocol:
    wait_for_final = False

    def __init__(self, config):
        self.assembler = TranscriptAssembler(identity_mode=config.identity_mode)

    async def send(self, ws, pcm):
        await ws.send_bytes(pack_asr(pcm, audio=True))

    async def finish(self, ws):
        await ws.send_bytes(pack_asr(b'', audio=True, final=True))

    def decode(self, message):
        from aiohttp import WSMsgType
        return unpack_asr(message.data) if message.type == WSMsgType.BINARY else None


class DeepgramProtocol:
    wait_for_final = True

    def __init__(self, config):
        from .deepgram import DeepgramAssembler
        self.assembler = DeepgramAssembler()

    async def send(self, ws, pcm):
        await ws.send_bytes(pcm)

    async def finish(self, ws):
        await ws.send_json({'type': 'CloseStream'})

    def decode(self, message):
        from aiohttp import WSMsgType
        if message.type == WSMsgType.TEXT:
            payload = message.json()
            return payload, payload.get('type') == 'Metadata'


class StreamingASRSession:
    def __init__(self, ws, protocol):
        self.ws, self.protocol = ws, protocol

    @property
    def close_code(self):
        return self.ws.close_code

    @property
    def wait_for_final(self):
        return self.protocol.wait_for_final

    def exception(self):
        return self.ws.exception()

    async def send_audio(self, pcm):
        await self.protocol.send(self.ws, pcm)

    async def finish(self):
        await self.protocol.finish(self.ws)

    async def close(self):
        await self.ws.close()

    async def results(self):
        from aiohttp import WSMsgType
        async for message in self.ws:
            if message.type in {WSMsgType.ERROR, WSMsgType.CLOSED, WSMsgType.CLOSE}:
                raise RuntimeError(f'ASR connection closed (code={self.close_code}, detail={self.exception()})')
            decoded = self.protocol.decode(message)
            if decoded is not None:
                payload, final = decoded
                yield self.protocol.assembler.accept(payload), final


async def open_stream(config, http, cloud):
    if config.asr_backend == 'deepgram':
        from .deepgram import DeepgramASR
        return StreamingASRSession(await DeepgramASR(config, http).open_asr(), DeepgramProtocol(config))
    if config.asr_backend == 'online':
        return StreamingASRSession(await cloud.open_asr(), VolcProtocol(config))
    raise ValueError('此识别后端不是云端流式接口')
