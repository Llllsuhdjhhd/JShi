"""Independent bounded rolling audio evidence. No ASR final or playback authority.

Window-local anonymous labels are deliberately not treated as persistent people.
Windows feed the independent recognition intake when on_window is attached; otherwise observational.
"""
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor


class AudioTimeline:
    # Window execution latency is not CPU consumption: inference threads can
    # consume CPU between calls. Measure paced cadence separately from wall time.
    def __init__(self, audio, analyzer, report, *, window_ms=8000, step_ms=1000, on_window=None):
        if not 0 < step_ms <= window_ms:
            raise ValueError('invalid audio analysis window')
        self.audio, self.analyzer, self.report = audio, analyzer, report
        self.window_ms, self.step_ms = window_ms, step_ms
        self.end_ms = self.covered_ms = 0
        self.revision = 0
        from .speaker_evidence import TrackLinker
        self.linker = TrackLinker()
        self.on_window = on_window
        self.segments = ()
        self.last_bounds = (0, 0)
        self.windows = deque(maxlen=32)
        self.wake = asyncio.Event()
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='audio-timeline')
        self.task = asyncio.create_task(self._run())

    def feed(self):
        if not self.closed:
            self.end_ms = (self.audio.offset + len(self.audio.data)) // 32
            if self.end_ms - self._computed_end() >= self.step_ms:
                self.wake.set()

    def _computed_end(self):
        return max(self.covered_ms, getattr(self.analyzer, 'computed_end_ms', 0))

    async def _run(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            # A demand from 04 may have filled the tail since feed() woke us.
            # Count actual model work, never a cache read, as the new cadence.
            self.covered_ms = self._computed_end()
            if self.end_ms-self.covered_ms < self.step_ms:
                continue
            # Only one executing window and one coalesced wakeup, never one job/packet.
            end = self.end_ms
            start = max(0, end - self.window_ms)
            if start > self.covered_ms:
                self.report('audio_timeline_gap', {'start_ms': self.covered_ms,
                    'end_ms': start, 'reason': 'analysis_backlog', 'shadow': self.on_window is None})
            pcm = self.audio.segment(start, end)
            if pcm is None:
                self.report('audio_timeline_gap', {'start_ms': start, 'end_ms': end,
                    'reason': 'audio_unavailable', 'shadow': self.on_window is None})
                self.covered_ms = end
                continue
            try:
                import numpy as np
                samples = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
                def analyze():
                    parts = self.analyzer.analyze(samples, start)
                    if getattr(self.analyzer, 'shared_segments', False):
                        # Keep a long ongoing turn intact until it really has a
                        # boundary; the 8-second inference window is not its start.
                        parts = self.analyzer.snapshot(max(0, end-self.analyzer.retain_ms), end)
                    identity = getattr(self.analyzer, 'identity', None)
                    if identity is not None:
                        identity.offer(parts, end)
                    return parts
                segments = await asyncio.get_running_loop().run_in_executor(self.executor, analyze)
                if not getattr(self.analyzer, 'shared_segments', False):
                    segments = self.linker.link(segments)
                self.segments = segments
                self.last_bounds = (start, end)
                self.revision += 1
                evidence = {'revision': self.revision, 'start_ms': start, 'end_ms': end,
                    'shadow': self.on_window is None, 'parts': [
                        {'start_ms': s.start_ms, 'end_ms': s.end_ms,
                         'window_track': s.track_id, 'voiceprint_id': s.voiceprint_id,
                         'overlap': s.overlap} for s in segments]}
                self.windows.append(evidence)
                self.report('audio_timeline_window', evidence)
                if self.on_window is not None:
                    await self.on_window(segments, start, end)
            except Exception as exc:
                self.report('audio_timeline_error', {'start_ms': start, 'end_ms': end,
                    'error_type': type(exc).__name__, 'shadow': self.on_window is None})
            self.covered_ms = max(end, self._computed_end())
            if self.end_ms - self.covered_ms >= self.step_ms:
                self.wake.set()

    async def close(self):
        self.closed = True
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
