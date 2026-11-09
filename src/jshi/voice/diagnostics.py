"""High-frequency voice observations kept out of the subject history database."""
import atexit
import json
import logging
import queue
import threading

# Observation streams: written per window or per ASR final. Rare failures and
# anything conversation logic reads back stay in subject history.
DIAGNOSTIC_EVENTS = frozenset({
    'cloud_alignment_timing', 'asr_batch_timing', 'voice_input_stage',
    'voice_identity_work', 'recovery_whole_revision',
    'voice_answer_deferred', 'recovery_event_revision', 'recovery_asr_timing', 'recovery_work_timing',
    'audio_timeline_window', 'audio_timeline_gap', 'audio_timeline_error',
    'utterance_organization', 'organized_turn_evidence', 'organized_turn_budget',
    'cloud_speaker_alignment', 'cloud_speaker_alignment_pending',
    'recovery_backlog', 'recovery_unaligned_revision', 'recovery_audio_missing',
    'recovery_interim', 'recovery_recognized', 'recovery_rate_limited', 'event_loop_lag', 'event_loop_blocked_stack',
})
MAX_BYTES = 20 * 1024 * 1024


class DiagnosticLog:
    def __init__(self, path):
        self.path = path
        self.rows = queue.Queue()
        self.thread = threading.Thread(target=self._run, name='jshi-voice-diagnostics', daemon=True)
        self.thread.start()

    def write(self, row):
        self.rows.put(row)

    def flush(self):
        if threading.current_thread() is not self.thread:
            self.rows.join()

    def _run(self):
        while True:
            batch = [self.rows.get()]
            while len(batch) < 256:
                try:
                    batch.append(self.rows.get_nowait())
                except queue.Empty:
                    break
            try:
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    self.path.replace(self.path.with_suffix('.previous.jsonl'))
                with self.path.open('a', encoding='utf-8') as stream:
                    for row in batch:
                        stream.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
            except Exception:
                logging.getLogger(__name__).exception('Voice diagnostics could not be saved')
            finally:
                for _ in batch:
                    self.rows.task_done()


async def watch_event_loop(report, *, interval_s=.25, window_s=10, threshold_ms=100):
    """Late wakeups delay ASR audio sends as well as result handling."""
    import asyncio
    from time import monotonic
    watchdog = LoopWatchdog(report, threading.get_ident())
    worst, late, started = 0.0, 0, monotonic()
    try:
        while True:
            expected = monotonic() + interval_s
            watchdog.beat()
            await asyncio.sleep(interval_s)
            lag = max(0.0, (monotonic() - expected) * 1000)
            worst = max(worst, lag)
            late += lag >= threshold_ms
            if monotonic() - started >= window_s:
                if worst >= threshold_ms:
                    report('event_loop_lag', {'window_s': round(monotonic() - started, 1),
                                              'max_ms': round(worst), 'late_wakeups': late})
                worst, late, started = 0.0, 0, monotonic()
    finally:
        watchdog.close()


class LoopWatchdog:
    """Sample the blocked loop from another thread, without locals or user data."""
    def __init__(self, report, loop_thread, *, threshold_s=1.0):
        from time import monotonic
        self.report, self.loop_thread, self.threshold_s = report, loop_thread, threshold_s
        self.last_beat = monotonic()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name='jshi-loop-watchdog', daemon=True)
        self.thread.start()

    def beat(self):
        from time import monotonic
        self.last_beat = monotonic()

    def _run(self):
        import sys
        from time import monotonic
        sampled, count = None, 0
        while not self.stop.wait(.25):
            beat = self.last_beat
            if beat != sampled:
                sampled, count = beat, 0
            lag = monotonic() - beat
            if lag < self.threshold_s * (count + 1) or count >= 3:
                continue
            frame = sys._current_frames().get(self.loop_thread)
            stack = []
            while frame is not None and len(stack) < 16:
                stack.append({'file': frame.f_code.co_filename, 'line': frame.f_lineno,
                              'function': frame.f_code.co_name})
                frame = frame.f_back
            del frame
            self.report('event_loop_blocked_stack', {'unresponsive_ms': round(lag*1000), 'stack': stack})
            count += 1

    def close(self):
        self.stop.set()
        self.thread.join(timeout=.3)


_logs = {}
_lock = threading.Lock()


def diagnostic_log(path):
    key = str(path.resolve())
    with _lock:
        if key not in _logs:
            _logs[key] = DiagnosticLog(path)
        return _logs[key]


@atexit.register
def _flush_all():
    for log in list(_logs.values()):
        log.flush()
