"""Provider-independent audio and transcript contracts. Times are session-relative ms."""
from dataclasses import dataclass
from typing import Any, Protocol, Iterable, AsyncIterator

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
BYTES_PER_MS = 32


@dataclass(frozen=True)
class AudioFrame:
    """Canonical capture output: 16kHz mono signed little-endian int16 PCM."""
    pcm: bytes
    start_sample: int

    def __post_init__(self):
        if len(self.pcm) % BYTES_PER_SAMPLE or self.start_sample < 0:
            raise ValueError('音频帧须为16位PCM，位置须为非负样本数')


@dataclass(frozen=True)
class SpeechSegment:
    samples: Any  # 16kHz mono float32; stays on the analysis/pipeline side
    track_id: str
    start_ms: int
    end_ms: int
    overlap: bool = False
    voiceprint_id: str = ''
    confidence: float | None = None
    speaker_cluster_id: str = ''
    identity_tentative: bool = False
    identity_note: str = ''

    def transcript(self, text):
        return Transcript(text, self.track_id, self.start_ms, self.end_ms, True,
            self.overlap, self.voiceprint_id, self.confidence,
            speaker_cluster_id=self.speaker_cluster_id,
            identity_tentative=self.identity_tentative, identity_note=self.identity_note)


class AudioSource(Protocol):
    def frames(self) -> Iterable[AudioFrame]: ...


class AudioAnalyzer(Protocol):
    def analyze(self, samples, offset_ms: int) -> tuple[SpeechSegment, ...]: ...


class Transcriber(Protocol):
    def transcribe_many(self, audios) -> list[str]: ...


class SpeakerEmbedder(Protocol):
    def embed(self, samples) -> list[float] | None: ...


class SpeakerIdentity(Protocol):
    def identify_timed(self, samples, *, source_track='', start_ms=None, end_ms=None): ...
    def reset_session(self): ...

@dataclass(frozen=True)
class TimedWord:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Transcript:
    text: str
    track_id: str
    start_ms: int
    end_ms: int
    final: bool
    overlap: bool = False
    voiceprint_id: str = ""
    confidence: float | None = None
    input_id: str = ""
    speaker_cluster_id: str = ""  # local anonymous cluster, not a named identity
    identity_note: str = ""
    identity_uncertain: bool = False
    identity_tentative: bool = False  # session continuity, not a fresh named match
    replaces: tuple[tuple[int, int], ...] = ()  # corrected audio ranges, not new speech
    audio_kind: str = 'original'  # original, mixed, or separately extracted source
    words: tuple[TimedWord, ...] = ()  # optional provider word times, session-relative ms
    replaces_input_ids: tuple[str, ...] = ()  # stable input association; word bounds may change




class StreamingRecognitionSession(Protocol):
    """Transport-neutral interface consumed by the microphone application.

    Providers own authentication, framing, acknowledgements, errors and final
    flushing. The caller only sends canonical PCM and reads common transcripts.
    """
    @property
    def close_code(self) -> int | None: ...
    @property
    def wait_for_final(self) -> bool: ...
    def exception(self) -> BaseException | None: ...
    async def send_audio(self, pcm: bytes) -> None: ...
    async def finish(self) -> None: ...
    async def close(self) -> None: ...
    def results(self) -> AsyncIterator[tuple[tuple[Transcript, ...], bool]]: ...
