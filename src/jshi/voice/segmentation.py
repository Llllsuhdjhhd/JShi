"""Streaming speech activity adapters. No transcription or speaker identity."""
from .contracts import SAMPLE_RATE
from time import monotonic
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SegmentSettings:
    silence_ms: int = 500
    max_window_s: float = 3.5


class SegmentPolicy(Protocol):
    def settings(self, config) -> SegmentSettings: ...


class FixedSegmentPolicy:
    def settings(self, config):
        return SegmentSettings(config.end_window_ms, config.max_window_s)


class PatientSegmentPolicy:
    def settings(self, config):
        return SegmentSettings(max(1000, config.end_window_ms), config.max_window_s)


SEGMENT_POLICIES = {'fixed':FixedSegmentPolicy(), 'patient':PatientSegmentPolicy()}


def segment_settings(config):
    return SEGMENT_POLICIES[config.segment_strategy].settings(config)


class SileroSegmenter:
    def __init__(self, detector, *, window_size=512, max_window_s=None):
        self.detector, self.window_size = detector, window_size
        self.pending = []
        self.total = 0
        self.processed = 0
        self.onset = None
        self.speech_from = None
        self.last_windows = []
        self.max_samples = int(max_window_s * SAMPLE_RATE) if max_window_s else None

    def speech_active(self):
        return getattr(self.detector, 'is_speech_detected', lambda: False)()

    def reset(self):
        self.detector.reset()
        self.pending = []
        self.total = 0
        self.processed = 0
        self.onset = None
        self.speech_from = None
        self.last_windows = []

    def feed(self, pcm, *, final=False):
        import numpy as np
        if len(pcm) % 2:
            raise ValueError('PCM须为16位小端格式')
        samples = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
        self.total += len(samples)
        values = np.concatenate((self.pending, samples)).astype('float32')
        cursor = 0
        output = []
        self.last_windows = []
        def drain():
            while not self.detector.empty():
                span = self.detector.front
                start = int(span.start)
                # Padding used to flush the detector is not real captured audio.
                end = min(self.total, start + len(span.samples))
                if end > start:
                    length = end-start
                    step = self.max_samples or length
                    cuts = list(range(0, length, step))
                    if len(cuts) > 1 and length-cuts[-1] < SAMPLE_RATE//4:
                        # Share a short tail with the previous window instead of a fragment.
                        cuts[-1] = (cuts[-2] + length + 1) // 2
                    for offset, stop in zip(cuts, cuts[1:]+[length]):
                        output.append((np.array(span.samples[offset:stop], dtype='float32'), (start+offset)*1000//SAMPLE_RATE))
                        self.last_windows.append({'start_ms':(start+offset)*1000//SAMPLE_RATE,
                            'end_ms':(start+stop)*1000//SAMPLE_RATE,
                            '_detected_mono':self.onset,
                            '_window_ready_mono':monotonic(),
                            'endpoint_audio_ms':min(self.total, self.processed)*1000//SAMPLE_RATE,
                            'cut_reason':'flush' if final else 'vad_endpoint_or_limit'})
                    self.onset = None
                self.detector.pop()
        while cursor + self.window_size <= len(values):
            self.detector.accept_waveform(values[cursor:cursor+self.window_size])
            self.processed += self.window_size
            detected = getattr(self.detector, 'is_speech_detected', lambda: False)()
            if detected and self.onset is None:
                self.onset = monotonic()
            if detected and self.speech_from is None:
                self.speech_from = self.processed
            elif not detected:
                self.speech_from = None
            cursor += self.window_size
            # sherpa-onnx max_speech_duration is not a hard cap: continuous
            # speech without a pause stays buffered until it ends. The margin
            # leaves room for the detector's pre-speech padding.
            if (detected and self.max_samples and hasattr(self.detector, 'flush')
                    and self.processed - self.speech_from >= self.max_samples - min(self.max_samples//2, SAMPLE_RATE*2//5)):
                self.detector.flush()
                self.speech_from = self.processed
            drain()
        self.pending = values[cursor:]
        if final:
            if len(self.pending):
                self.detector.accept_waveform(np.pad(self.pending, (0, self.window_size-len(self.pending))))
                self.pending = []
            self.detector.flush()
            drain()
        return tuple(output)
