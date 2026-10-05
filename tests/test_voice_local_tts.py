from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest

from jshi.voice.config import VoiceConfig
from jshi.voice.local_tts import LocalTTS


def test_local_backends_need_no_voice_key_but_online_does(monkeypatch):
    monkeypatch.delenv("JSHI_VOICE_API_KEY", raising=False)
    monkeypatch.delenv("JSHI_VOICE_IDENTITY_MODE", raising=False)
    config = VoiceConfig.from_env(asr="local", tts="local")
    assert not config.api_key
    with pytest.raises(ValueError, match="JSHI_VOICE_API_KEY"):
        VoiceConfig.from_env(asr="local", tts="online")
    with pytest.raises(ValueError, match="JSHI_VOICE_API_KEY"):
        VoiceConfig.from_env(asr="online", tts="local")


def test_local_tts_uses_model_rate_and_bounded_phrases():
    np = pytest.importorskip("numpy")
    calls = []
    class Engine:
        sample_rate = 44100
        def generate(self, text, **kwargs):
            calls.append((text, kwargs))
            return SimpleNamespace(samples=np.array([-2., 0., 2.], dtype="float32"))
    async def run():
        tts = LocalTTS(Engine(), VoiceConfig("", asr_backend="local", tts_backend="local"))
        chunks = [pcm async for pcm in tts.synthesize("甲" * 65 + "，你好。")]
        assert tts.config.template.sample_rate == 44100
        assert all(len(text) <= 60 for text, _ in calls)
        assert "".join(text for text, _ in calls) == "甲" * 65 + "，你好。"
        assert all(kwargs == {"sid": 0, "speed": .95} for _, kwargs in calls)
        assert np.frombuffer(chunks[0], dtype="<i2").tolist() == [-32767, 0, 32767]
    asyncio.run(run())


def test_cancelled_synthesis_discards_inflight_audio_and_later_phrases():
    np = pytest.importorskip("numpy")
    entered, release, finished = Event(), Event(), Event()
    calls = []
    class Engine:
        sample_rate = 24000
        def generate(self, text, **kwargs):
            calls.append(text)
            entered.set()
            release.wait(3)
            finished.set()
            return SimpleNamespace(samples=np.ones(20, dtype="float32"))
    async def run():
        tts = LocalTTS(Engine(), VoiceConfig("", asr_backend="local", tts_backend="local"))
        audio = []
        async def collect():
            async for chunk in tts.synthesize("第一句。第二句。"):
                audio.append(chunk)
        task = asyncio.create_task(collect())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
            assert calls == ["第一句。"]
            assert not audio
        finally:
            release.set()
    asyncio.run(run())
