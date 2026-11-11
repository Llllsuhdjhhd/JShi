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
        self.pending = {}

    def offer(self, source, key, description):
        # Evidence itself stays in shared scene; this is only a wakeup marker.
        self.pending[source] = WakeSignal(source, key, description)

    def claim(self):
        now = self.clock()
        if not self.enabled or now < self.next_allowed:
            return ()
        if now - self.last_review >= self.seconds:
            self.offer("timer", "timer", f"距上次自主审视约{int(now-self.last_review)}秒；没有新增人物发言。")
        batch = tuple(self.pending.values())
        self.pending.clear()
        return batch

    def finish(self, batch, *, success):
        now = self.clock()
        self.next_allowed = now + min(30., self.seconds)
        if success:
            self.last_review = now
        else:
            for signal in batch:
                self.pending.setdefault(signal.source, signal)


def tool_mode(conversation_active):
    return "conversation" if conversation_active else "initiative"
