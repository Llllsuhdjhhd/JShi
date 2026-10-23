"""Bounded session-relative PCM for matching cloud utterances locally."""
from threading import RLock
from time import time
from uuid import uuid4
import sqlite3


class SessionAudio:
    def __init__(self, seconds=45, *, archive=None):
        self.limit = int(seconds * 32000)  # mono 16 kHz int16
        self.data = bytearray()
        self.offset = 0
        self.lock = RLock()
        self.started_at_ms = None
        self.archive = archive
        self.capture_id = uuid4().hex
        self.archive_error = ''

    def append(self, pcm):
        with self.lock:
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
            if self.started_at_ms is None and pcm:
                self.started_at_ms = time() * 1000 - len(pcm) / 32
            self.data.extend(pcm)
            excess = max(0, len(self.data) - self.limit)
            if excess:
                del self.data[:excess]
                self.offset += excess

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
