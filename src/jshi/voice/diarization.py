"""Compatibility facade composing independent audio analysis and transcription."""
from .analysis import SpeakerAnalyzer
from .pipeline import transcribe_segments


class LocalDiarizer(SpeakerAnalyzer):
    def __init__(self, engine, speakers, recognizer, *, build_engine=None, model=''):
        super().__init__(engine, speakers, build_engine=build_engine, model=model)
        self.recognizer = recognizer

    def transcribe(self, samples, offset_ms):
        return transcribe_segments(self.analyze(samples, offset_ms), self.recognizer)
