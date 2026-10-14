from __future__ import annotations

import os
from dataclasses import dataclass, field
from jshi.core.params import VOICE_INPUT_PAUSE_MS, VISUAL_TURN_IDLE_SECONDS


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
    input_pause_s: float = VOICE_INPUT_PAUSE_MS / 1000
    environment_idle_s: float = VISUAL_TURN_IDLE_SECONDS
    candidate_timeout_s: float = 1.2
    input_max_batch_s: float = 12.0
    input_batch_chars: int = 20
    deepgram_api_key: str = field(default='', repr=False)
    deepgram_model: str = 'nova-3'
    deepgram_language: str = 'zh-CN'
    deepgram_diarize: bool = True

    def __post_init__(self) -> None:
        if self.asr_backend not in {"online", "local", "deepgram"}:
            raise ValueError("voice ASR backend must be online, local or deepgram")
        if not self.deepgram_model.strip() or not self.deepgram_language.strip():
            raise ValueError('Deepgram model and language must be nonempty')
        if self.asr_backend == 'deepgram' and self.identity_mode == 'voiceprint':
            raise ValueError('Deepgram使用说话人分段；不支持火山云端voiceprint模式，请设置diarization')
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
        if not 0 <= self.environment_idle_s <= 60:
            raise ValueError('JSHI_VOICE_ENVIRONMENT_IDLE_MS must be between 0 and 60000')
        if not .3 <= self.input_max_batch_s <= 15:
            raise ValueError('JSHI_VOICE_INPUT_MAX_BATCH_MS must be between 300 and 15000')
        if not 0 <= self.input_batch_chars <= 10000:
            raise ValueError('JSHI_VOICE_INPUT_BATCH_CHARS must be between 0 and 10000')
        if not .1 <= self.candidate_timeout_s <= 5:
            raise ValueError("JSHI_VOICE_CANDIDATE_TIMEOUT_MS must be between 100 and 5000")

    @classmethod
    def from_env(cls, *, asr: str | None = None, tts: str | None = None) -> VoiceConfig:
        key = os.getenv("JSHI_VOICE_API_KEY", "").strip()
        asr = asr or os.getenv("JSHI_VOICE_ASR", "online")
        tts = tts or os.getenv("JSHI_VOICE_TTS", "online")
        deepgram_key = os.getenv('JSHI_DEEPGRAM_API_KEY', '').strip()
        missing = []
        if not key and "online" in {asr, tts}:
            uses = '、'.join(name for value,name in ((asr,'识别（ASR）'),(tts,'播报（TTS）')) if value == 'online')
            missing.append(f'火山{uses}已选online，需要JSHI_VOICE_API_KEY')
        if asr == 'deepgram' and not deepgram_key:
            missing.append('Deepgram识别（ASR）需要JSHI_DEEPGRAM_API_KEY')
        if missing:
            raise ValueError('请补充 .env 语音配置：'+'；'.join(missing)+'。识别与播报独立配置。')
        diarize = os.getenv('JSHI_DEEPGRAM_DIARIZE', 'true').strip().lower()
        if diarize not in {'true','false','1','0','on','off'}:
            raise ValueError('JSHI_DEEPGRAM_DIARIZE必须为true或false')
        return cls(
            api_key=key,
            deepgram_api_key=deepgram_key,
            deepgram_model=os.getenv('JSHI_DEEPGRAM_MODEL','nova-3').strip(),
            deepgram_language=os.getenv('JSHI_DEEPGRAM_LANGUAGE','zh-CN').strip(),
            deepgram_diarize=diarize in {'true','1','on'},
            asr_backend=asr,
            tts_backend=tts,
            asr_resource=os.getenv("JSHI_VOICE_ASR_RESOURCE", "volc.seedasr.sauc.duration"),
            tts_resource=os.getenv("JSHI_VOICE_TTS_RESOURCE", "seed-tts-2.0"),
            voiceprint_group=os.getenv("JSHI_VOICEPRINT_GROUP", ""),
            identity_mode=os.getenv("JSHI_VOICE_IDENTITY_MODE", "diarization"),
            end_window_ms=int(os.getenv("JSHI_VOICE_END_WINDOW_MS", "500")),
            tts_transport=os.getenv('JSHI_VOICE_TTS_TRANSPORT', 'websocket'),
            input_pause_s=int(os.getenv('JSHI_VOICE_INPUT_PAUSE_MS', str(VOICE_INPUT_PAUSE_MS))) / 1000,
            environment_idle_s=int(os.getenv('JSHI_VOICE_ENVIRONMENT_IDLE_MS', str(int(VISUAL_TURN_IDLE_SECONDS*1000)))) / 1000,
            input_max_batch_s=int(os.getenv('JSHI_VOICE_INPUT_MAX_BATCH_MS', '12000')) / 1000,
            input_batch_chars=int(os.getenv('JSHI_VOICE_INPUT_BATCH_CHARS', '20')),
            candidate_timeout_s=int(os.getenv('JSHI_VOICE_CANDIDATE_TIMEOUT_MS', '1200')) / 1000,
            template=VoiceTemplate(
                speaker=os.getenv("JSHI_VOICE_SPEAKER", "zh_female_vv_uranus_bigtts"),
                speech_rate=int(os.getenv("JSHI_VOICE_RATE", "-5")),
                direction=os.getenv("JSHI_VOICE_DIRECTION", VoiceTemplate.direction),
            ),
        )
