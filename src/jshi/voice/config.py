from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class VoiceTemplate:
    speaker: str = "zh_female_vv_uranus_bigtts"
    speech_rate: int = -5
    sample_rate: int = 24000
    direction: str = "自然、平静地交谈，吐字清楚，语气亲切，不要播音腔。"

    def __post_init__(self) -> None:
        if not self.speaker or not -50 <= self.speech_rate <= 100:
            raise ValueError("invalid voice speaker or speech rate")


@dataclass(frozen=True)
class VoiceConfig:
    api_key: str = field(repr=False)
    asr_backend: str = "online"
    tts_backend: str = "online"
    asr_resource: str = "volc.seedasr.sauc.duration"
    tts_resource: str = "seed-tts-2.0"
    voiceprint_group: str = ""
    identity_mode: str = "diarization"  # multi-speaker or single-speaker voiceprint
    template: VoiceTemplate = VoiceTemplate()
    jev_timeout_s: float = 3.0
    end_window_ms: int = 500
    tts_transport: str = 'websocket'
    input_pause_s: float = 2.5
    candidate_timeout_s: float = 1.2
    input_max_batch_s: float = 6.0

    def __post_init__(self) -> None:
        if self.asr_backend not in {"online", "local"}:
            raise ValueError("voice ASR backend must be online or local")
        if self.tts_backend not in {"online", "local"}:
            raise ValueError("voice TTS backend must be online or local")
        if self.identity_mode not in {"diarization", "voiceprint"}:
            raise ValueError("voice identity mode must be diarization or voiceprint")
        if self.identity_mode == "voiceprint" and not self.voiceprint_group:
            raise ValueError("voiceprint mode requires JSHI_VOICEPRINT_GROUP")
        if not 300 <= self.end_window_ms <= 5000:
            raise ValueError("JSHI_VOICE_END_WINDOW_MS must be between 300 and 5000")
        if self.tts_transport not in {'websocket', 'sse'}:
            raise ValueError('voice TTS transport must be websocket or sse')
        if not .3 <= self.input_pause_s <= 15:
            raise ValueError('JSHI_VOICE_INPUT_PAUSE_MS must be between 300 and 15000')
        if not .3 <= self.input_max_batch_s <= 15:
            raise ValueError('JSHI_VOICE_INPUT_MAX_BATCH_MS must be between 300 and 15000')
        if not .1 <= self.candidate_timeout_s <= 5:
            raise ValueError("JSHI_VOICE_CANDIDATE_TIMEOUT_MS must be between 100 and 5000")

    @classmethod
    def from_env(cls, *, asr: str | None = None, tts: str | None = None) -> VoiceConfig:
        key = os.getenv("JSHI_VOICE_API_KEY", "").strip()
        asr = asr or os.getenv("JSHI_VOICE_ASR", "online")
        tts = tts or os.getenv("JSHI_VOICE_TTS", "online")
        if not key and "online" in {asr, tts}:
            raise ValueError("请在 .env 配置 JSHI_VOICE_API_KEY（火山语音服务 API Key）")
        return cls(
            api_key=key,
            asr_backend=asr,
            tts_backend=tts,
            asr_resource=os.getenv("JSHI_VOICE_ASR_RESOURCE", "volc.seedasr.sauc.duration"),
            tts_resource=os.getenv("JSHI_VOICE_TTS_RESOURCE", "seed-tts-2.0"),
            voiceprint_group=os.getenv("JSHI_VOICEPRINT_GROUP", ""),
            identity_mode=os.getenv("JSHI_VOICE_IDENTITY_MODE", "diarization"),
            end_window_ms=int(os.getenv("JSHI_VOICE_END_WINDOW_MS", "500")),
            tts_transport=os.getenv('JSHI_VOICE_TTS_TRANSPORT', 'websocket'),
            input_pause_s=int(os.getenv('JSHI_VOICE_INPUT_PAUSE_MS', '2500')) / 1000,
            input_max_batch_s=int(os.getenv('JSHI_VOICE_INPUT_MAX_BATCH_MS', '6000')) / 1000,
            candidate_timeout_s=int(os.getenv('JSHI_VOICE_CANDIDATE_TIMEOUT_MS', '1200')) / 1000,
            template=VoiceTemplate(
                speaker=os.getenv("JSHI_VOICE_SPEAKER", "zh_female_vv_uranus_bigtts"),
                speech_rate=int(os.getenv("JSHI_VOICE_RATE", "-5")),
                direction=os.getenv("JSHI_VOICE_DIRECTION", VoiceTemplate.direction),
            ),
        )
