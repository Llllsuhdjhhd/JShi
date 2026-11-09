"""Shared, bounded acoustic work for rolling recovery and final word alignment."""
from dataclasses import replace
from threading import RLock
from time import monotonic

from .speaker_evidence import TrackLinker
from .timing import report_safely, timed_lock


class SegmentationCache:
    shared_segments = True

    def __init__(self, audio, analyzer, report=None, *, retain_ms=120000):
        self.audio, self.analyzer, self.report = audio, analyzer, report
        self.retain_ms = retain_ms
        self.lock = RLock()
        self.windows = []
        self.linker = TrackLinker()
        self.computed_end_ms = 0

    def analyze(self, samples, start):
        """Called by 06; publish acoustic evidence before anyone uses its words."""
        with timed_lock(self.lock, 'shared_acoustic_lock_ms'):
            end = start + len(samples) // 16
            # Reuse an exact window even if 04 was the first requester.
            for a, b, parts in self.windows:
                if (a, b) == (start, end):
                    return self._read(start, end)[0]
            began = monotonic()
            parts = self.analyzer.analyze(samples, start)
            # A late final can request an older gap. Link against overlapping
            # cached audio, not the last window's local speaker label numbers.
            self.linker.previous = self._read(start, end)[0]
            parts = self.linker.link(parts)
            self.windows.append((start, end, parts))
            self.computed_end_ms = max(self.computed_end_ms, end)
            newest = max(b for _, b, _ in self.windows)
            self.windows = [w for w in self.windows if w[1] > newest-self.retain_ms][-160:]
            report_safely(self.report, 'voice_input_stage', {
                'stage': 'shared_acoustic_window', 'source_start_ms': start,
                'source_end_ms': end, 'elapsed_ms': round((monotonic()-began)*1000, 3)})
            return self._read(start, end)[0]

    def snapshot(self, start, end):
        """Read accumulated evidence only; never trigger work or advance cadence."""
        with self.lock:
            return self._read(start, end)[0]

    def continuous_voice(self, track, start, end):
        """Prove continuity from accumulated acoustic evidence, not a label alone.

        Missing analysis, a different voice or any overlap breaks reuse. Silence
        inside fully analyzed audio is not itself evidence of a different person.
        """
        with self.lock:
            parts, gaps = self._read(start, end)
            if gaps or not parts or end <= start:
                return False
            cursor = start
            for part in parts:
                if part.overlap or part.track_id != track:
                    return False
                cursor = max(cursor, part.end_ms)
            return cursor >= end

    @staticmethod
    def _clip(part, start, end):
        a, b = max(start, part.start_ms), min(end, part.end_ms)
        if b <= a:
            return None
        return replace(part, start_ms=a, end_ms=b,
                       samples=part.samples[(a-part.start_ms)*16:(b-part.start_ms)*16])

    def _read(self, start, end):
        candidates = [(a, b, parts) for a, b, parts in self.windows if a < end and b > start]
        cuts = sorted({start, end, *(max(start, a) for a, _, _ in candidates),
                       *(min(end, b) for _, b, _ in candidates),
                       # Split inside edge guards, not just at window endpoints.
                       # Otherwise [1,8] ties two windows and lets 1–9 erase a
                       # boundary already observed inside 0–8.
                       *(max(start, min(end, a+500)) for a, _, _ in candidates),
                       *(max(start, min(end, b-500)) for _, b, _ in candidates),
                       *(max(start, min(end, t)) for _, _, parts in candidates
                         for p in parts for t in (p.start_ms, p.end_ms))})
        selected, gaps = [], []
        for a, b in zip(cuts, cuts[1:]):
            covers = [(i, w) for i, w in enumerate(candidates) if w[0] <= a and w[1] >= b]
            if not covers:
                if gaps and gaps[-1][1] == a:
                    gaps[-1] = (gaps[-1][0], b)
                else:
                    gaps.append((a, b))
                continue
            # Prefer the interior of a contextual window. Newest-only selection
            # splices its unreliable leading edge into previously covered words
            # (a real replay produced a false 30ms gap in the word "匠").
            _, window = max(covers, key=lambda item: (
                min(500, a-item[1][0], item[1][1]-b), item[1][1]-item[1][0], item[0]))
            selected.extend(p for part in window[2] if (p := self._clip(part, a, b)) is not None)
        # Join only adjacent pieces of the same linked, non-overlapping voice.
        result = []
        for part in sorted(selected, key=lambda s: (s.start_ms, s.end_ms, s.track_id)):
            if (result and not part.overlap and not result[-1].overlap
                    and part.track_id == result[-1].track_id and result[-1].end_ms == part.start_ms):
                import numpy as np
                previous = result[-1]
                result[-1] = replace(previous, end_ms=part.end_ms,
                                     samples=np.concatenate((previous.samples, part.samples)))
            else:
                result.append(part)
        return tuple(result), gaps

    def for_range(self, start, end):
        """04 reuses covered audio and computes only missing chunks, at most 8 s."""
        with timed_lock(self.lock, 'shared_acoustic_lock_ms'):
            with self.audio.lock:
                available_start = self.audio.offset // 32
                available_end = (self.audio.offset + len(self.audio.data)) // 32
            # Providers round endpoints to a frame. Do not manufacture samples
            # or alter word timestamps for a <=20ms rounded tail.
            actual_end = min(end, available_end)
            if end > available_end+20 or actual_end <= start:
                raise ValueError('原声缺口，无法补齐声音时间线')
            parts, gaps = self._read(start, actual_end)
            missing_ms = sum(b-a for a, b in gaps)
            for a, b in gaps:
                if a < available_start:
                    raise ValueError('未缓存且原声已移出窗口，无法补齐声音时间线')
                cursor = a
                while cursor < b:
                    target_end = min(cursor+6000, b)
                    # Context is for the model only; output stays inside the
                    # requested span. No fake silence or changed word clocks.
                    # Up to two seconds of left context for a short missing tail;
                    # large missing spans still keep total model input <= 8 s.
                    right = min(available_end, target_end+1000)
                    left = max(available_start, cursor-2000, right-8000)
                    pcm = self.audio.segment(left, right)
                    if pcm is None or right <= left:
                        raise ValueError('原声缺口，无法补齐声音时间线')
                    import numpy as np
                    self.analyze(np.frombuffer(pcm, dtype='<i2').astype('float32')/32768, left)
                    cursor = target_end
            parts, remaining = self._read(start, actual_end)
            if remaining:
                raise ValueError('声音时间线未覆盖完整原声')
            report_safely(self.report, 'voice_input_stage', {
                'stage': 'shared_acoustic_reuse', 'source_start_ms': start,
                'source_end_ms': end, 'reused_ms': actual_end-start-missing_ms,
                'missing_ms': missing_ms, 'rounded_tail_ms': end-actual_end})
            return parts
