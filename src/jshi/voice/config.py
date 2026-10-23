from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from jshi.core.params import VOICE_INPUT_PAUSE_MS, VISUAL_TURN_IDLE_SECONDS
from .backends import ASR_BACKENDS

ASR_HOTWORDS = ('陆广', '陆兴', '秦奋', '广', '广广', 'JEV', '匠石', '匠匠', '声纹', '未来城', '匠')


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
    jev_timeout_s: float = 5.0
    end_window_ms: int = 500
    max_window_s: float = 3.5
    segment_strategy: str = 'fixed'
    turn_strategy: str = 'smart_turn'
    turn_threshold: float = .5
    turn_timeout_s: float = .6
    turn_max_hold_s: float = 2
    semantic_turn_strategy: str = 'namo'
    semantic_turn_threshold: float = .5
    semantic_turn_timeout_s: float = .2
    asr_hotwords: tuple[str, ...] = ASR_HOTWORDS
    tts_transport: str = 'websocket'
    # Legacy collection settings: accepted for configuration compatibility only.
    # Ready final transcripts are dispatched immediately; these do not add a wait.
    input_pause_s: float = VOICE_INPUT_PAUSE_MS / 1000
    environment_idle_s: float = VISUAL_TURN_IDLE_SECONDS
    candidate_timeout_s: float = 1.2
    input_max_batch_s: float = 12.0
    input_batch_chars: int = 20
    deepgram_api_key: str = field(default='', repr=False)
    deepgram_model: str = 'nova-3'
    deepgram_language: str = 'zh-CN'
    deepgram_diarize: bool = True
    separation_backend: str = 'off'
    separation_device: str = 'cpu'
    separation_timeout_s: float = 30.0
    qwen_asr_url: str = 'http://127.0.0.1:8000/v1'
    qwen_asr_model: str = ''
    qwen_asr_timeout_s: float = 60.0
    qwen_asr_batch_size: int = 8

    def __post_init__(self) -> None:
        from .segmentation import SEGMENT_POLICIES
        from .turns import TURN_POLICIES
        from .semantic_turn import SEMANTIC_POLICIES
        if self.segment_strategy not in SEGMENT_POLICIES or self.turn_strategy not in TURN_POLICIES:
            raise ValueError('未知音频切段或整句判断策略')
        if self.semantic_turn_strategy not in SEMANTIC_POLICIES:
            raise ValueError('JSHI_VOICE_SEMANTIC_TURN须为namo或off')
        if not 0 <= self.semantic_turn_threshold <= 1 or not .01 <= self.semantic_turn_timeout_s <= 2:
            raise ValueError('语义阈值须为0–1，语义等待预算须为10–2000ms')
        if not .5 <= self.max_window_s <= 30 or not 0 <= self.turn_threshold <= 1:
            raise ValueError('处理窗须为0.5–30秒，整句概率阈值须为0–1')
        if not .01 <= self.turn_timeout_s <= 2 or not .1 <= self.turn_max_hold_s <= 10:
            raise ValueError('整句超时须为10–2000ms，未完句保留须为100–10000ms')
        if self.separation_backend not in {'off', 'pyannote'}:
            raise ValueError('JSHI_VOICE_SEPARATION须为off或pyannote')
        if not 1 <= self.separation_timeout_s <= 120:
            raise ValueError('声音分离最长等待须为1–120秒')
        if self.separation_backend != 'off' and self.asr_backend != 'qwen' and not self.api_key:
            raise ValueError('分离音轨独立转写需要JSHI_VOICE_API_KEY')
        if self.asr_backend not in ASR_BACKENDS:
            raise ValueError("voice ASR backend must be online, local, deepgram or qwen")
        if not 1 <= self.qwen_asr_batch_size <= 32 or not 1 <= self.qwen_asr_timeout_s <= 300:
            raise ValueError('Qwen ASR批次大小须为1–32，超时须为1–300秒')
        if self.asr_backend == 'qwen' and self.identity_mode == 'voiceprint':
            raise ValueError('Qwen请使用diarization模式；云端voiceprint仅用于火山')
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
        if not 1 <= self.jev_timeout_s <= 20:
            raise ValueError("JSHI_VOICE_JEV_TIMEOUT_MS must be between 1000 and 20000")

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
            qwen_asr_url=os.getenv('JSHI_QWEN_ASR_URL', 'http://127.0.0.1:8000/v1').rstrip('/'),
            qwen_asr_model=os.getenv('JSHI_QWEN_ASR_MODEL', '').strip(),
            qwen_asr_timeout_s=float(os.getenv('JSHI_QWEN_ASR_TIMEOUT_S', '60')),
            qwen_asr_batch_size=int(os.getenv('JSHI_QWEN_ASR_BATCH_SIZE', '8')),
            separation_backend=os.getenv('JSHI_VOICE_SEPARATION', 'off'),
            separation_device=os.getenv('JSHI_VOICE_SEPARATION_DEVICE', 'cpu'),
            separation_timeout_s=float(os.getenv('JSHI_VOICE_SEPARATION_TIMEOUT_S', '30')),
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
            max_window_s=float(os.getenv('JSHI_VOICE_MAX_WINDOW_MS', '3500'))/1000,
            segment_strategy=os.getenv('JSHI_VOICE_SEGMENT_STRATEGY', 'fixed'),
            turn_strategy=os.getenv('JSHI_VOICE_TURN_STRATEGY', 'smart_turn'),
            turn_threshold=float(os.getenv('JSHI_VOICE_TURN_THRESHOLD', '.5')),
            turn_timeout_s=float(os.getenv('JSHI_VOICE_TURN_TIMEOUT_MS', '600'))/1000,
            turn_max_hold_s=float(os.getenv('JSHI_VOICE_TURN_MAX_HOLD_MS', '2000'))/1000,
            semantic_turn_strategy=os.getenv('JSHI_VOICE_SEMANTIC_TURN','namo'),
            semantic_turn_threshold=float(os.getenv('JSHI_VOICE_SEMANTIC_THRESHOLD','.5')),
            semantic_turn_timeout_s=float(os.getenv('JSHI_VOICE_SEMANTIC_TIMEOUT_MS','200'))/1000,
            asr_hotwords=tuple(dict.fromkeys(w.strip() for w in re.split(r'[,，;；\n]',
                os.getenv('JSHI_VOICE_ASR_HOTWORDS', ','.join(ASR_HOTWORDS))) if w.strip())),
            tts_transport=os.getenv('JSHI_VOICE_TTS_TRANSPORT', 'websocket'),
            input_pause_s=int(os.getenv('JSHI_VOICE_INPUT_PAUSE_MS', str(VOICE_INPUT_PAUSE_MS))) / 1000,
            environment_idle_s=int(os.getenv('JSHI_VOICE_ENVIRONMENT_IDLE_MS', str(int(VISUAL_TURN_IDLE_SECONDS*1000)))) / 1000,
            input_max_batch_s=int(os.getenv('JSHI_VOICE_INPUT_MAX_BATCH_MS', '12000')) / 1000,
            input_batch_chars=int(os.getenv('JSHI_VOICE_INPUT_BATCH_CHARS', '20')),
            candidate_timeout_s=int(os.getenv('JSHI_VOICE_CANDIDATE_TIMEOUT_MS', '1200')) / 1000,
            jev_timeout_s=float(os.getenv('JSHI_VOICE_JEV_TIMEOUT_MS', '5000')) / 1000,
            template=VoiceTemplate(
                speaker=os.getenv("JSHI_VOICE_SPEAKER", "zh_female_vv_uranus_bigtts"),
                speech_rate=int(os.getenv("JSHI_VOICE_RATE", "-5")),
                direction=os.getenv("JSHI_VOICE_DIRECTION", VoiceTemplate.direction),
            ),
        )
