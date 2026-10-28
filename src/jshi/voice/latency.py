"""Diagnostic clocks only; never used to decide turns or sent to models."""
from collections import deque
from time import monotonic
import json
from threading import Lock

_log_lock = Lock()


def append_latency(path, row):
    """Bound diagnostic files; do not mix traces into conversational memory."""
    with _log_lock:
        if path.exists() and path.stat().st_size > 10 * 1024 * 1024:
            path.replace(path.with_suffix('.previous.jsonl'))
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')


def elapsed_ms(end, start):
    return round(max(0, end - start) * 1000, 1) if end is not None and start is not None else None


class AudioReceiptClock:
    """Map audio offsets to actual Windows packet arrivals, not estimated wall time."""
    def __init__(self):
        self.samples = 0
        self.packets = deque(maxlen=2400)  # four minutes at 100ms/packet

    def append(self, pcm, at=None):
        count = len(pcm) // 2
        if count:
            self.packets.append((self.samples, self.samples + count, monotonic() if at is None else at))
            self.samples += count

    def at_ms(self, offset_ms):
        sample = offset_ms * 16
        for start, end, at in self.packets:
            if start <= sample < end:
                return at
        return None


def public_timing(timing, now=None):
    """Durations with explicit scopes; private monotonic anchors stay server-side."""
    now = timing.get('_text_sent_mono') if now is None else now
    result = {k:v for k,v in timing.items() if not k.startswith('_')}
    result.update({
        'detected_to_text_sent_ms': elapsed_ms(now, timing.get('_detected_mono')),
        'audio_start_received_to_text_sent_ms': elapsed_ms(now, timing.get('_audio_start_received_mono')),
        'audio_end_received_to_text_sent_ms': elapsed_ms(now, timing.get('_audio_end_received_mono')),
        'audio_end_received_to_result_ready_ms': elapsed_ms(timing.get('_result_ready_mono'), timing.get('_audio_end_received_mono')),
        'result_ready_to_text_sent_ms': elapsed_ms(now, timing.get('_result_ready_mono')),
        'asr_return_to_text_sent_ms': elapsed_ms(now, timing.get('_asr_return_mono')),
        'detected_to_asr_return_ms': elapsed_ms(timing.get('_asr_return_mono'), timing.get('_detected_mono')),
    })
    return result
