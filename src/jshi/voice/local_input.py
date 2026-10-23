"""Receive local PCM without blocking browser control messages on decoding."""
import asyncio
from functools import partial
from time import monotonic
from .latency import AudioReceiptClock, elapsed_ms


class LocalAudioPump:
    def __init__(self, decoder, executor, accept, on_error=None, *, accept_batch=None, on_timings=None, on_turn_state=None):
        self.decoder, self.executor, self.accept = decoder, executor, accept
        self.accept_batch = accept_batch
        self.on_error = on_error
        self.on_timings = on_timings
        self.on_turn_state = on_turn_state
        self.receipts = AudioReceiptClock()
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
        self.receipts.append(pcm)
        self.wake.set()

    async def _decode(self, pcm, final=False):
        loop = asyncio.get_running_loop()
        transcripts = await loop.run_in_executor(
            self.executor, partial(self.decoder.feed, pcm, final=final))
        if self.on_turn_state and getattr(self.decoder, 'last_previews', ()):
            await self.on_turn_state(self.decoder.last_previews, getattr(self.decoder, 'last_turn_events', []),
                                     getattr(self.decoder, 'last_preview_timings', []))
        timings = getattr(self.decoder, 'last_timings', [])
        if self.on_timings and len(timings) == len(transcripts):
            for timing in timings:
                timing['_audio_start_received_mono'] = self.receipts.at_ms(timing['start_ms'])
                timing['_audio_end_received_mono'] = self.receipts.at_ms(max(timing['start_ms'], timing['end_ms'] - .0625))
                endpoint = timing.get('endpoint_audio_ms')
                endpoint_received = self.receipts.at_ms(endpoint - .0625) if endpoint is not None and endpoint > 0 else None
                timing['endpoint_backlog_ms'] = elapsed_ms(timing.get('_window_ready_mono'), endpoint_received)
                timing['audio_end_to_window_ready_ms'] = elapsed_ms(timing.get('_window_ready_mono'), timing['_audio_end_received_mono'])
                timing['decode_return_to_event_loop_ms'] = elapsed_ms(monotonic(), timing.get('_pipeline_return_mono', timing.get('_asr_return_mono')))
                timing['audio_start_to_detection_ms'] = elapsed_ms(timing.get('_detected_mono'),
                    self.receipts.at_ms(timing.get('window_start_ms') if timing.get('window_start_ms') is not None else timing['start_ms']))
                timing['pending_audio_ms_at_return'] = round(len(self.pending) / 32, 1)
            self.on_timings(transcripts, timings)
        if self.accept_batch is not None:
            await self.accept_batch(transcripts)
        else:
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
