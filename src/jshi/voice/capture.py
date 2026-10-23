"""Capture adapters produce canonical frames, without models or speaker state."""
import wave
from .contracts import AudioFrame, SAMPLE_RATE


class PcmCapture:
    """Push source for browser, device SDK or network PCM (already normalized)."""
    def __init__(self):
        self.position = 0

    def accept(self, pcm):
        frame = AudioFrame(bytes(pcm), self.position)
        self.position += len(frame.pcm) // 2
        return frame


class WavCapture:
    """Pull source for normalized WAV files. Never guess the raw audio format."""
    def __init__(self, path, *, frame_samples=1600):
        if frame_samples <= 0:
            raise ValueError('音频帧长度须大于0')
        self.path, self.frame_samples = path, frame_samples

    def frames(self):
        with wave.open(str(self.path), 'rb') as source:
            if (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getcomptype()) != (SAMPLE_RATE, 1, 2, 'NONE'):
                raise ValueError('文件须为16kHz、单声道、16位PCM WAV，请先转换格式')
            capture = PcmCapture()
            while pcm := source.readframes(self.frame_samples):
                yield capture.accept(pcm)
