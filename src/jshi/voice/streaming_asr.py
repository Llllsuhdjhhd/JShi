"""Cloud streaming protocol adapters exposing one session interface to the app."""
from .volc import pack_asr, unpack_asr
from .transcripts import TranscriptAssembler
from .contracts import StreamingRecognitionSession


async def buffered_results(session, *, capacity=64, report=None):
    """Read cloud results independently of downstream identity/turn analysis.

    Preserve order and arrival clocks. Bound memory and surface overflow instead
    of silently losing speech or building an unlimited stale queue.
    """
    import asyncio
    from time import monotonic, time
    from .timing import report_safely
    queue = asyncio.Queue(maxsize=capacity)

    async def pump():
        async for rows, final in session.results():
            received, at = monotonic(), time()*1000
            report_safely(report, 'asr_batch_timing', {'state': 'received',
                'received_at_ms': at, 'queued_batches': queue.qsize(),
                'parts': [{'start_ms': r.start_ms, 'end_ms': r.end_ms,
                           'final': r.final} for r in rows] if report is not None else []})
            try:
                queue.put_nowait((rows, final, received, at))
            except asyncio.QueueFull:
                raise RuntimeError('语音处理积压，识别结果队列已满，请重新开始对话')
            if final:
                break

    task = asyncio.create_task(pump())
    get = None
    def consumed(item):
        report_safely(report, 'asr_batch_timing', {'state': 'consumed',
            'received_at_ms': item[3], 'buffer_wait_ms': round((monotonic()-item[2])*1000, 3),
            'queued_batches': queue.qsize(),
            'parts': [{'start_ms': r.start_ms, 'end_ms': r.end_ms,
                       'final': r.final} for r in item[0]] if report is not None else []})
        return item
    try:
        while True:
            if not queue.empty():
                yield consumed(queue.get_nowait())
                continue
            if task.done():
                await task
                break
            get = asyncio.create_task(queue.get())
            await asyncio.wait((task, get), return_when=asyncio.FIRST_COMPLETED)
            if get.done():
                yield consumed(get.result())
                get = None
            else:
                get.cancel()
                await asyncio.gather(get, return_exceptions=True)
                get = None
    finally:
        task.cancel()
        if get is not None:
            get.cancel()
            await asyncio.gather(get, return_exceptions=True)
        await asyncio.gather(task, return_exceptions=True)


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


async def open_stream(config, http, cloud) -> StreamingRecognitionSession:
    if config.asr_backend == 'bailian':
        from .bailian import open_bailian
        return await open_bailian(config, http)
    if config.asr_backend == 'deepgram':
        from .deepgram import DeepgramASR
        return StreamingASRSession(await DeepgramASR(config, http).open_asr(), DeepgramProtocol(config))
    if config.asr_backend == 'online':
        return StreamingASRSession(await cloud.open_asr(), VolcProtocol(config))
    raise ValueError('此识别后端不是云端流式接口')
