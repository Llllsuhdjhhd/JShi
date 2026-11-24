"""Mac MLX speech adapters. Inference runs in the separate local-ai environment."""
import asyncio
from collections import deque
from dataclasses import replace
import io
import wave
from time import monotonic

from .contracts import Transcript
from .asr_text import clean_asr_text
from .timing import report_safely

SERVER = 'http://127.0.0.1:18766'
BASE = SERVER + '/v1/audio'
ASR_MODEL = 'mlx-community/Qwen3-ASR-1.7B-8bit'
TTS_MODEL = 'mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit'


async def audio_ready():
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=.5)) as http:
            async with http.get(SERVER + '/') as response:
                return response.status == 200 and 'MLX Audio' in (await response.json()).get('message', '')
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError):
        return False


class MlxASR:
    """Adapt local Qwen audio snapshots to the microphone streaming contract."""
    wait_for_final = True
    close_code = None
    frame_bytes = 640  # 20 ms of mono PCM16 at 16 kHz
    trailing_context_ms = 80

    def __init__(self, config, http):
        import webrtcvad
        self.http = http
        self.report = None
        self.vad = webrtcvad.Vad(2)
        self.silence_ms = config.end_window_ms
        self.limit_ms = int(config.max_window_s * 1000)
        self.pending = bytearray()
        self.wake = asyncio.Event()
        self.ending = False
        self.closed = False
        self.lead = deque(maxlen=8)
        self.utterance = bytearray()
        self.offset = self.quiet = 0
        self.preview = None
        self.preview_pcm = b''
        self.preview_end_ms = self.last_preview_offset = 0
        self.preview_sent = False
        self.displayed_text = ''

    def exception(self):
        return None

    async def send_audio(self, pcm):
        if self.ending or self.closed:
            raise RuntimeError('本地识别会话已结束')
        if len(self.pending) + len(pcm) > 32 * 32000:
            raise BufferError('本地识别积压超过32秒')
        self.pending.extend(pcm)
        self.wake.set()

    async def finish(self):
        self.ending = True
        self.wake.set()

    async def _discard_preview(self):
        if self.preview is not None:
            self.preview.cancel()
            await asyncio.gather(self.preview, return_exceptions=True)
            self.preview = None
            self.preview_pcm = b''

    async def close(self):
        self.closed = True
        self.wake.set()
        await self._discard_preview()

    async def transcribe(self, pcm):
        import aiohttp
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', ''))
            wav.writeframes(pcm)
        form = aiohttp.FormData()
        form.add_field('file', buf.getvalue(), filename='input.wav', content_type='audio/wav')
        for key, value in {'model': ASR_MODEL, 'language': 'zh', 'response_format': 'json'}.items():
            form.add_field(key, value)
        async with self.http.post(BASE+'/transcriptions', data=form, timeout=60) as response:
            response.raise_for_status()
            return clean_asr_text((await response.json())['text'])

    def _row(self, text, end_ms, final):
        start = (self.offset - len(self.utterance)) // 32
        return Transcript(text, f'mlx-{start}', start, max(start+1, end_ms),
                          final, identity_uncertain=True)

    def _feed_frame(self, frame):
        self.offset += len(frame)
        speech = self.vad.is_speech(frame.ljust(self.frame_bytes, b'\0'), 16000)
        if not self.utterance and not speech:
            self.lead.append(frame)
            return False
        if not self.utterance:
            self.utterance.extend(b''.join(self.lead))
            self.lead.clear()
            self.last_preview_offset = self.offset
        self.utterance.extend(frame)
        self.quiet = 0 if speech else self.quiet + 20
        return self.quiet >= self.silence_ms or len(self.utterance) >= self.limit_ms * 32

    def _asr_input(self):
        # The endpoint detector can wait longer than the decoder needs for
        # trailing context. A pause *inside* the utterance is retained in full.
        tail = max(0, self.quiet - self.trailing_context_ms) * 32
        return bytes(self.utterance[:-tail] if tail else self.utterance)

    async def _preview_text(self, pcm):
        try:
            return await self.transcribe(pcm)
        except Exception as exc:
            report_safely(self.report, 'mlx_asr_preview_failed', {'error_type': type(exc).__name__})
            return None

    def _start_preview(self):
        # Decode real audio only. Continue collecting every frame so any new
        # speech, including an internal pause, is present in the final input.
        if self.preview is not None and not self.preview.done():
            return
        end_ms = self.offset // 32 - self.quiet
        at_pause = (self.quiet >= self.trailing_context_ms and
                    self.last_preview_offset < (end_ms + self.trailing_context_ms) * 32)
        if not at_pause and (self.quiet or self.offset - self.last_preview_offset < 32000):
            return
        pcm = self._asr_input()
        self.preview_pcm = pcm
        self.preview_end_ms = end_ms
        self.last_preview_offset = self.offset
        self.preview_sent = False
        self.preview = asyncio.create_task(self._preview_text(pcm))
        self.preview.add_done_callback(lambda _: self.wake.set())

    async def _final(self):
        endpoint_at = monotonic()
        text = None
        if self.preview is not None and self.preview_pcm == self._asr_input():
            text = await self.preview
        await self._discard_preview()
        reused = bool(text)
        if not reused:
            text = await self.transcribe(bytes(self.utterance))
        report_safely(self.report, 'mlx_asr_final', {
            'endpoint_audio_ms': self.offset // 32, 'tail_silence_ms': self.quiet,
            'preview_reused': reused, 'endpoint_to_final_ms': round((monotonic()-endpoint_at)*1000, 1),
        })
        # Clear an obsolete UI hypothesis even if the final decode is empty.
        row = None
        if text or self.displayed_text:
            row = self._row(text, self.offset // 32 - self.quiet, bool(text))
        self.displayed_text = ''
        self.utterance.clear()
        self.quiet = 0
        return row

    async def results(self):
        while not self.closed:
            await self.wake.wait()
            self.wake.clear()
            if self.preview is not None and self.preview.done() and not self.preview_sent:
                self.preview_sent = True
                text = self.preview.result()
                if text:
                    self.displayed_text = text
                    yield (self._row(text, self.preview_end_ms, False),), False
            while len(self.pending) >= self.frame_bytes or (self.ending and self.pending):
                frame = bytes(self.pending[:self.frame_bytes])
                del self.pending[:len(frame)]
                if self._feed_frame(frame):
                    row = await self._final()
                    if row:
                        yield (row,), False
                elif self.utterance:
                    self._start_preview()
            if self.ending:
                row = await self._final() if self.utterance else None
                yield ((row,) if row else ()), True
                return


class MlxTTS:
    def __init__(self, config, http):
        self.http = http
        self.config = replace(config, template=replace(config.template, sample_rate=24000))

    async def synthesize(self, text):
        # This fixed Vivian model emits mono PCM16 at 24 kHz. Four codec frames
        # provide 320 ms of sound per generation step.
        payload = {'model': TTS_MODEL, 'input': text, 'voice': 'Vivian',
                   'response_format': 'pcm', 'stream': True, 'streaming_interval': 0.32}
        async with self.http.post(BASE+'/speech', json=payload) as response:
            response.raise_for_status()
            if response.content_type != 'audio/pcm':
                raise ValueError('MLX 合成返回的内容不是 PCM 音频')
            tail = b''
            received = False
            async for chunk in response.content.iter_chunked(4800):
                data = tail + chunk
                # HTTP chunks do not have to end on a 16-bit sample boundary.
                size = len(data) - len(data) % 2
                if size:
                    received = True
                    yield data[:size]
                tail = data[size:]
            if tail or not received:
                raise ValueError('MLX 合成返回的 PCM 音频不完整')
