"""Coalesced review opportunities, separate from durable task/delivery state."""
from dataclasses import dataclass
from time import monotonic
import os

@dataclass(frozen=True)
class WakeSignal:
    source: str
    key: str
    description: str

class WakeQueue:
    def __init__(self, *, clock=monotonic, seconds=None):
        self.clock = clock
        self.seconds = max(1., float(seconds if seconds is not None else os.getenv("JSHI_IDLE_SECONDS", "180")))
        self.enabled = os.getenv("JSHI_IDLE_DISABLE", "").lower() not in {"true", "1", "yes", "on"}
        self.last_review = clock()
        self.next_allowed = self.last_review
        self.next_event_allowed = self.last_review
        self.pending = {}
        self.last_fingerprints = {}
        self.quiet_rounds = 0

    @property
    def interval(self):
        return min(self.seconds * 2 ** max(0, self.quiet_rounds - 1), max(self.seconds, 1800.))

    def note_activity(self):
        """A human interaction or reviewed result makes an old timer obsolete."""
        self.last_review = self.clock()
        self.quiet_rounds = 0
        self.next_allowed = self.last_review + min(30., self.seconds)
        self.pending.pop('timer', None)

    def offer(self, source, key, description, *, fingerprint=None):
        # Evidence itself stays in shared scene; this is only a wakeup marker.
        if fingerprint is not None:
            if self.last_fingerprints.get(source) == fingerprint:
                return
            self.last_fingerprints[source] = fingerprint
        self.pending[source] = WakeSignal(source, key, description)
        if source != 'timer':
            self.quiet_rounds = 0
            self.pending.pop("timer", None)

    def claim(self, *, allow_timer=True):
        now = self.clock()
        if not self.enabled:
            return ()
        events = tuple(signal for signal in self.pending.values() if signal.source != 'timer')
        if events:
            if now < self.next_event_allowed:
                return ()
            self.pending.pop('timer', None)
            batch = events
        else:
            if not allow_timer or now < self.next_allowed:
                return ()
            if now - self.last_review >= self.interval:
                self.offer("timer", "timer", f"距上次交谈或审视约{int(now-self.last_review)}秒；本轮没有新增人物发言。")
            batch = tuple(self.pending.values())
        for signal in batch:
            self.pending.pop(signal.source, None)
        return batch

    def finish(self, batch, *, success, quiet=False):
        now = self.clock()
        self.next_allowed = now + min(30., self.seconds)
        self.next_event_allowed = self.next_allowed
        if success:
            self.last_review = now
            unchanged = all(x.source == 'timer' for x in batch) and not self.pending
            self.quiet_rounds = min(self.quiet_rounds + 1, 16) if quiet and unchanged else 0
        else:
            for signal in batch:
                if signal.source == 'timer' and now - self.last_review < self.interval:
                    continue  # A newer human turn/result has already reset this timer.
                self.pending.setdefault(signal.source, signal)


def tool_mode(conversation_active):
    return "conversation" if conversation_active else "initiative"
