"""Bounded session-relative PCM for matching cloud utterances locally."""
from threading import RLock
from time import time, monotonic
from uuid import uuid4
import sqlite3
from .latency import AudioReceiptClock


class SessionAudio:
    def __init__(self, seconds=45, *, archive=None):
        self.limit = int(seconds * 32000)  # mono 16 kHz int16
        self.data = bytearray()
        self.offset = 0
        self.lock = RLock()
        self.started_at_ms = None  # Legacy sample-clock estimate, not a latency anchor.
        self.started_mono = None
        self.receipt_wall = AudioReceiptClock()
        self.receipt_mono = AudioReceiptClock()
        self.archive = archive
        self.capture_id = uuid4().hex
        self.archive_error = ''

    def append(self, pcm, *, received_at_ms=None, received_mono=None):
        wall = time() * 1000 if received_at_ms is None else received_at_ms
        mono = monotonic() if received_mono is None else received_mono
        with self.lock:
            self.receipt_wall.append(pcm, at=wall)
            self.receipt_mono.append(pcm, at=mono)
            if self.started_at_ms is None and pcm:
                self.started_at_ms = wall - len(pcm) / 32
                self.started_mono = mono - len(pcm) / 32000
            if self.archive is not None and pcm and not self.archive_error:
                position = self.offset+len(self.data)
                enqueue = getattr(self.archive, 'enqueue', None)
                try:
                    if enqueue is not None:
                        enqueue(self.capture_id, position, pcm, self._archive_failed)
                    else:
                        self.archive.append(self.capture_id, position, pcm)
                except (OSError, sqlite3.Error) as exc:
                    self.archive_error = str(exc)
            self.data.extend(pcm)
            excess = max(0, len(self.data) - self.limit)
            if excess:
                del self.data[:excess]
                self.offset += excess

    def clock_drift_ms(self, now=None):
        """Cumulative capture-clock difference; never a sentence latency."""
        with self.lock:
            if self.started_mono is None:
                return None
            return round(((monotonic() if now is None else now) - self.started_mono) * 1000
                         - (self.offset + len(self.data)) / 32, 1)

    def receipt_timing(self, start_ms, end_ms):
        """Actual packet arrivals, to packet resolution; evicted anchors remain unknown."""
        with self.lock:
            end_sample_ms = end_ms - 1 / 16 if end_ms > start_ms else None
            return {
                'audio_start_received_at_ms': self.receipt_wall.at_ms(start_ms),
                'audio_end_received_at_ms': self.receipt_wall.at_ms(end_sample_ms) if end_sample_ms is not None else None,
                '_audio_start_received_mono': self.receipt_mono.at_ms(start_ms),
                '_audio_end_received_mono': self.receipt_mono.at_ms(end_sample_ms) if end_sample_ms is not None else None,
            }

    def _archive_failed(self, error):
        # Runs on the archive writer thread; taking self.lock here could deadlock segment().
        self.archive_error = error

    def segment(self, start_ms, end_ms):
        with self.lock:
            start, end = start_ms * 32 - self.offset, end_ms * 32 - self.offset
            if start < 0 or end > len(self.data) or end <= start:
                return self.archive.segment(self.capture_id,start_ms,end_ms) if self.archive else None
            return bytes(self.data[start:end])

    def match(self, transcript, speakers, *, pcm=None):
        # Compatibility for callers predating the separate analysis stage.
        from .matching import match_audio
        return match_audio(self, transcript, speakers, pcm=pcm)
