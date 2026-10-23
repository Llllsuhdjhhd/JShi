"""Audio processing orchestration; recognition engines are injected."""
from __future__ import annotations
from contextlib import nullcontext
import json
from time import monotonic
from .latency import elapsed_ms
from .contracts import Transcript
from .asr_text import clean_asr_text
from .transcription import as_transcriber


class SpeechPipeline:
    """Compose a speech detector, an audio analyzer and any text-only transcriber."""
    def __init__(self, detector, analyzer, transcriber, *, backend, turns=None):
        self.detector, self.analyzer, self.transcriber = detector, analyzer, transcriber
        self.backend = backend
        self.last_timings = []
        self.turns = turns
        self.last_previews = ()
        self.last_preview_timings = []
        self.last_turn_events = []

    @property
    def speakers(self):
        return self.analyzer.speakers

    @speakers.setter
    def speakers(self, speakers):
        self.analyzer.speakers = speakers

    @property
    def diarizer(self):
        return self.analyzer

    @property
    def refiner(self):
        return self.transcriber

    def reset_session(self):
        self.detector.reset()
        self.last_timings = []
        self.last_previews = ()
        if self.turns:
            self.turns.reset()
        if self.speakers is not None:
            self.speakers.reset_session()

    def feed(self, pcm, *, final=False):
        self.last_timings = []
        segments = []
        began = monotonic()
        windows = self.detector.feed(pcm, final=final)
        vad_done = monotonic()
        for samples, start_ms in windows:
            segments.extend(self.analyzer.analyze(samples, start_ms))
        analyzed = monotonic()
        ticket = self.turns.begin(segments) if self.turns and segments else None
        rows = transcribe_segments(segments, self.transcriber) if segments else ()
        returned = monotonic()
        for row in rows:
            window = next((w for w in getattr(self.detector, 'last_windows', [])
                if w['start_ms'] <= row.start_ms < w['end_ms']), {})
            self.last_timings.append({**window, 'backend':self.backend,
                'start_ms':row.start_ms, 'end_ms':row.end_ms,
                'window_start_ms':window.get('start_ms'), 'window_end_ms':window.get('end_ms'),
                'vad_compute_ms':elapsed_ms(vad_done, began),
                'analysis_batch_ms':elapsed_ms(analyzed, vad_done),
                'transcription_batch_ms':elapsed_ms(returned, analyzed),
                'batch_segments':len(segments), '_asr_return_mono':returned,
                **getattr(self.transcriber, 'last_timing', {})})
        self.last_previews = rows
        self.last_preview_timings = list(self.last_timings)
        self.last_turn_events = []
        if self.turns:
            if ticket is not None:
                rows, self.last_timings = self.turns.finish(ticket, segments, rows, self.last_timings, final=final)
                self.last_turn_events = self.turns.last_events
            else:
                ready, timings = [], []
                self.turns.poll(ready, timings, final=final,
                    allow_expiry=not getattr(self.detector, 'speech_active', lambda:False)())
                rows, self.last_timings = tuple(ready), timings
        finished = monotonic()
        self.last_timings = [{**t,'_pipeline_return_mono':finished} for t in self.last_timings]
        return rows

    def close(self):
        if self.turns:
            self.turns.close()

class LocalASR:
    def __init__(self, recognizer, speakers=None, refiner=None, diarizer=None) -> None:
        self.recognizer, self.speakers = recognizer, speakers
        self.refiner = refiner
        self.diarizer = diarizer
        self.early_speaker = None
        self.stream = recognizer.create_stream()
        self.offset_samples = 0
        self.segment_samples = 0
        self.samples = []
        self.last_text = ""

    def reset_session(self) -> None:
        self.stream = self.recognizer.create_stream()
        self.offset_samples = self.segment_samples = 0
        self.samples.clear()
        self.last_text = ""
        self.early_speaker = None
        if self.speakers:
            self.speakers.tracks.clear()
            self.speakers.track_samples.clear()
            self.speakers.pending_tracks.clear()
            self.speakers.last_embedding.clear()
            self.speakers.binding.clear()
            self.speakers.reset_session()

    def feed(self, pcm: bytes, *, final: bool = False) -> tuple[Transcript, ...]:
        import numpy as np
        if len(pcm) % 2:
            raise ValueError("PCM must be signed 16-bit little endian")
        values = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768
        if len(values):
            self.stream.accept_waveform(16000, values)
            self.samples.append(values)
            self.segment_samples += len(values)
        if final:
            self.stream.accept_waveform(16000, np.zeros(8000, dtype="float32"))
            self.stream.input_finished()
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        text = clean_asr_text(self.recognizer.get_result(self.stream))
        endpoint = final or self.recognizer.is_endpoint(self.stream) or self.segment_samples >= 16000 * (6 if self.diarizer else 25)
        start, end = self.offset_samples // 16, (self.offset_samples+self.segment_samples) // 16
        output = []
        if endpoint and (text or (self.refiner is not None and self.samples)):
            audio = np.concatenate(self.samples) if self.samples else np.array([], dtype="float32")
            if self.diarizer is not None and len(audio):
                output.extend(self.diarizer.transcribe(audio, start))
                text = ""
            elif self.refiner is not None and len(audio):
                text = as_transcriber(self.refiner).transcribe_many([audio])[0]
            text = clean_asr_text(text)
            if text:
                with getattr(self.speakers, 'lock', nullcontext()):
                    track, vpid, confidence = self.speakers.identify_timed(audio, start_ms=start, end_ms=end) if self.speakers else (f"unidentified-{start}", "", None)
                    match = dict(getattr(self.speakers, 'last_match', {}))
                    note = json.dumps(match, ensure_ascii=False) if self.speakers else ''
                output.append(Transcript(text, track, start, end, True, voiceprint_id=vpid, confidence=confidence, speaker_cluster_id=track if self.speakers and not track.startswith('unidentified-') else '', identity_tentative=bool(self.speakers and not vpid and not track.startswith('unidentified-') and match.get('tentative', True)), identity_note=note))
        elif (text or self.last_text) and text != self.last_text:
            if self.speakers and self.diarizer is None and self.segment_samples >= 24000 and self.early_speaker is None:
                self.early_speaker = self.speakers.identify(np.concatenate(self.samples))
            track, vpid, score = self.early_speaker or (f"pending-{start}", "", None)
            output.append(Transcript(text, track, start, end, False, voiceprint_id=vpid, confidence=score))
        self.last_text = text
        if endpoint and not final:
            self.recognizer.reset(self.stream)
            self.offset_samples += self.segment_samples
            self.segment_samples = 0
            self.samples.clear()
            self.last_text = ""
            self.early_speaker = None
        return tuple(output)



def transcribe_segments(segments, engine):
    segments = tuple(segments)
    texts = as_transcriber(engine).transcribe_many([s.samples for s in segments])
    if len(texts) != len(segments):
        raise ValueError('转写结果数量与音频片段不一致')
    return tuple(s.transcript(clean_asr_text(text)) for s, text in zip(segments, texts)
                 if clean_asr_text(text))


def transcribe_capture(source, decoder):
    """Run any pull capture through an existing configured streaming pipeline."""
    expected = 0
    decoder.reset_session()
    for frame in source.frames():
        if frame.start_sample != expected:
            raise ValueError("音频帧时间不连续，不能跨采集会话拼接")
        expected += len(frame.pcm) // 2
        yield from decoder.feed(frame.pcm)
    yield from decoder.feed(b"", final=True)
