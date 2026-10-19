"""Receive local PCM without blocking browser control messages on decoding."""
import asyncio
from functools import partial


class LocalAudioPump:
    def __init__(self, decoder, executor, accept, on_error=None):
        self.decoder, self.executor, self.accept = decoder, executor, accept
        self.on_error = on_error
        self.pending = bytearray()
        self.wake = asyncio.Event()
        self.closing = False
        self.error = None
        self.task = asyncio.create_task(self._run())

    def feed(self, pcm):
        if self.error:
            raise self.error
        if self.closing:
            raise RuntimeError('local audio input already closed')
        # Never silently discard or reorder accepted speech. Surface overload.
        if len(self.pending) + len(pcm) > 32 * 32000:
            raise BufferError('本地识别积压超过32秒，请暂停输入或切换识别服务')
        self.pending.extend(pcm)
        self.wake.set()

    async def _decode(self, pcm, final=False):
        loop = asyncio.get_running_loop()
        transcripts = await loop.run_in_executor(
            self.executor, partial(self.decoder.feed, pcm, final=final))
        for transcript in transcripts:
            await self.accept(transcript)

    async def _run(self):
        try:
            while True:
                await self.wake.wait()
                while self.pending:
                    # Coalesce queued tiny browser frames, with bounded chunks so
                    # the decoder still observes endpoints instead of one huge batch.
                    pcm = bytes(self.pending[:16000])
                    del self.pending[:len(pcm)]
                    await self._decode(pcm)
                if self.closing:
                    await self._decode(b'', final=True)
                    return
                self.wake.clear()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
            if self.on_error:
                await self.on_error(exc)

    async def finish(self):
        self.closing = True
        self.wake.set()
        await self.task
        if self.error:
            raise self.error
