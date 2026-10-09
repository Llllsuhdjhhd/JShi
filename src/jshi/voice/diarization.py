"""Time-stamped speaker segmentation. This does not extract clean audio sources."""
from __future__ import annotations

from .volc import Transcript
from .asr_text import clean_asr_text


class LocalDiarizer:
    def __init__(self, engine, speakers, recognizer, *, build_engine=None, model=''):
        self.engine, self.speakers, self.recognizer = engine, speakers, recognizer
        self.engines = {model: engine} if model else {}
        self.build_engine = build_engine

    def use(self, model, speakers):
        """Segment clustering and identity matching must share one embedding space."""
        if self.build_engine is not None and model not in self.engines:
            self.engines[model] = self.build_engine(model)
        self.engine = self.engines.get(model, self.engine)
        self.speakers = speakers

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
            text = clean_asr_text(stream.result.text)
            if not text:
                continue
            if overlap:
                # Mixed source audio cannot safely enroll or identify a person.
                track, vpid, confidence = f"overlap-{offset_ms}-{i}", "", None
            elif hasattr(self.speakers, 'identify_timed'):
                track, vpid, confidence = self.speakers.identify_timed(audio, source_track=str(span.speaker),
                    start_ms=offset_ms + start // 16, end_ms=offset_ms + end // 16)
            else:
                track, vpid, confidence = self.speakers.identify(audio)
            output.append(Transcript(text, track, offset_ms + start // 16, offset_ms + end // 16,
                                     True, overlap, vpid, confidence,
                                     speaker_cluster_id=track if not overlap and not track.startswith('unidentified-') else '',
                                     identity_tentative=not overlap and not vpid and not track.startswith('unidentified-')
                                         and getattr(self.speakers, 'last_match', {}).get('tentative', True)))
        return tuple(output)
