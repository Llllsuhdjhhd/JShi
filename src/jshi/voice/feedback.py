"""Shared permission and cooldown for three kinds of short feedback."""
from time import monotonic


class FeedbackArbiter:
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.last = None
        self.revision = 0

    def changed(self):
        self.revision += 1

    def claim(self, kind, revision, *, speaking=False, busy=False, overlap=False, enabled=False):
        if kind not in {'listening_ack', 'waiting_ack', 'reconsider_ack'}:
            return False
        # Listening feedback requires a real-time acoustic gap signal; ASR alone is too late.
        if kind == 'listening_ack' or not enabled or speaking or busy or overlap or revision != self.revision:
            return False
        now = self.clock()
        if self.last is not None and now-self.last < 30:
            return False
        self.last = now
        return True
