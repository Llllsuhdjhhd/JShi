"""Time-stamped speaker segmentation. This does not extract clean audio sources."""
from __future__ import annotations

from .volc import Transcript


class LocalDiarizer:
    def __init__(self, engine, speakers, recognizer):
        self.engine, self.speakers, self.recognizer = engine, speakers, recognizer

    def transcribe(self, samples, offset_ms):
        spans = self.engine.process(samples).sort_by_start_time()
        output = []
        for i, span in enumerate(spans):
            start = max(0, int(span.start * 16000))
            end = min(len(samples), int(span.end * 16000))
            if end <= start:
                continue
            overlap = any(j != i and span.speaker != other.speaker and min(span.end, other.end) - max(span.start, other.start) > .05
                          for j, other in enumerate(spans))
            audio = samples[start:end]
            stream = self.recognizer.create_stream()
            stream.accept_waveform(16000, audio)
            self.recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            if not text:
                continue
            if overlap:
                # Mixed source audio cannot safely enroll or identify a person.
                track, vpid, confidence = f"overlap-{offset_ms}-{i}", "", None
            else:
                track, vpid, confidence = self.speakers.identify(audio)
            output.append(Transcript(text, track, offset_ms + start // 16, offset_ms + end // 16,
                                     True, overlap, vpid, confidence))
        return tuple(output)
