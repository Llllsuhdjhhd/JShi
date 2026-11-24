import asyncio
import sys
from types import SimpleNamespace

from jshi.voice.mlx import MlxASR


# Deterministic 20 ms frames let the test place the last word on either side of
# the pause. No microphone, model server, or optional VAD dependency is needed.
VOICE = b'\x01\x00' * 320
QUIET = b'\0' * 640


def session(monkeypatch, decode):
    vad = SimpleNamespace(is_speech=lambda frame, rate: frame != QUIET)
    monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(Vad=lambda mode: vad))
    asr = MlxASR(SimpleNamespace(end_window_ms=300, max_window_s=3.5), None)
    asr.transcribe = decode
    return asr


def test_pause_can_preview_but_only_confirmed_audio_is_final(monkeypatch):
    async def run():
        calls = []
        async def decode(pcm):
            calls.append(pcm)
            return '你好'
        asr = session(monkeypatch, decode)
        async with asyncio.timeout(2):
            results = asr.results()
            await asr.send_audio(VOICE + QUIET * 4)
            (preview,), end = await anext(results)
            assert not end and not preview.final
            await asr.send_audio(QUIET * 11)
            (final,), end = await anext(results)
            assert not end and final.final and final.text == preview.text
            assert (final.start_ms, final.end_ms) == (0, 20)
            await asr.finish()
            assert await anext(results) == ((), True)
            assert len(calls) == 1
            await results.aclose()
            await asr.close()
    asyncio.run(run())


def test_resumed_speech_survives_disconnect_flush(monkeypatch):
    async def run():
        resumed = b'\x02\x00' * 320
        calls = []
        async def decode(pcm):
            calls.append(pcm)
            return '你好，请继续' if resumed in pcm else '你好'
        asr = session(monkeypatch, decode)
        async with asyncio.timeout(2):
            results = asr.results()
            await asr.send_audio(VOICE + QUIET * 4)
            (preview,), _ = await anext(results)
            assert preview.text == '你好' and not preview.final
            # End the capture halfway through an ordinary 20 ms frame.
            await asr.send_audio(resumed + resumed[:160])
            await asr.finish()
            (final,), end = await anext(results)
            assert end and final.final and final.text == '你好，请继续'
            assert final.track_id == preview.track_id
            assert calls[-1] == VOICE + QUIET * 4 + resumed + resumed[:160]
            await results.aclose()
            await asr.close()
    asyncio.run(run())


def test_failed_preview_retries_full_recording(monkeypatch):
    async def run():
        attempts = []
        async def decode(pcm):
            attempts.append(pcm)
            if len(attempts) == 1:
                raise OSError('preview unavailable')
            return '你好'
        asr = session(monkeypatch, decode)
        async with asyncio.timeout(2):
            results = asr.results()
            await asr.send_audio(VOICE + QUIET * 15)
            (final,), end = await anext(results)
            assert not end and final.final and final.text == '你好'
            assert attempts == [VOICE + QUIET * 4, VOICE + QUIET * 15]
            await results.aclose()
            await asr.close()
    asyncio.run(run())
