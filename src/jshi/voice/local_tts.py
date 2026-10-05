"""CPU preset synthesis; cancellation suppresses unfinished phrase audio."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import re
from threading import Event, Lock

from jshi.voice.config import VoiceConfig
from jshi.voice.local import TTS_NAME


class LocalTTS:
    def __init__(self, engine, config: VoiceConfig) -> None:
        self.engine = engine
        self.config = replace(config, template=replace(config.template, sample_rate=engine.sample_rate))
        self.lock = Lock()

    async def synthesize(self, text: str):
        import numpy as np
        cancelled = Event()
        speed = 1 + self.config.template.speech_rate / 100

        def generate(phrase):
            # A cancelled asyncio wait cannot forcibly interrupt ONNX inference.
            # Serialize inference and discard both queued and completed stale work.
            with self.lock:
                if cancelled.is_set():
                    return None
                return self.engine.generate(phrase, sid=0, speed=speed)

        try:
            phrases = re.findall(r"[^，,；;。！？!?\n]+[，,；;。！？!?\n]*", text)
            for phrase in phrases:
                for start in range(0, len(phrase), 60):
                    audio = await asyncio.to_thread(generate, phrase[start:start + 60])
                    if audio is None or cancelled.is_set():
                        return
                    pcm = (np.clip(audio.samples, -1, 1) * 32767).astype("<i2").tobytes()
                    if not pcm:
                        raise RuntimeError("本地合成没有返回声音")
                    chunk_size = max(2, self.config.template.sample_rate // 10 * 2)
                    for offset in range(0, len(pcm), chunk_size):
                        yield pcm[offset:offset + chunk_size]
                        await asyncio.sleep(0)
        finally:
            cancelled.set()


def build_local_tts(data_dir: Path, config: VoiceConfig) -> LocalTTS:
    try:
        import sherpa_onnx
    except ImportError:
        raise ValueError('本地合成需要安装：python -m pip install -e ".[voice-local]"')
    root = Path(os.getenv("JSHI_VOICE_LOCAL_DIR", str(data_dir / "voice_models"))) / TTS_NAME
    for name in ("model.onnx", "tokens.txt", "lexicon.txt"):
        if not (root / name).is_file():
            raise ValueError("本地合成音色尚未准备，请先运行 jshi voice-setup --tts。")
    threads = max(1, min(4, int(os.getenv("JSHI_VOICE_LOCAL_THREADS", "2"))))
    rules = ",".join(str(root / name) for name in ("phone.fst", "date.fst", "number.fst") if (root / name).is_file())
    model = sherpa_onnx.OfflineTtsModelConfig(
        vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(root / "model.onnx"),
            tokens=str(root / "tokens.txt"), lexicon=str(root / "lexicon.txt")),
        num_threads=threads, provider="cpu",
    )
    settings = sherpa_onnx.OfflineTtsConfig(model=model, rule_fsts=rules, max_num_sentences=1)
    if not settings.validate():
        raise ValueError("本地合成模型配置无效")
    return LocalTTS(sherpa_onnx.OfflineTts(settings), config)
