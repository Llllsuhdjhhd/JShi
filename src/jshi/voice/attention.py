"""Conversation focus is a decision hint, never an acoustic identity score."""
from __future__ import annotations

from time import monotonic


class ConversationAttention:
    def __init__(self, *, ttl_s=90.0, clock=monotonic):
        self.ttl_s, self.clock = ttl_s, clock
        self.focus: dict[str, float] = {}

    def engage(self, object_ids):
        now = self.clock()
        self.focus = {k: t for k, t in self.focus.items() if now - t < self.ttl_s}
        for object_id in object_ids:
            if object_id:
                self.focus[object_id] = now

    def hint(self, speaker, *, overlap=False):
        age = self.clock() - self.focus.get(speaker.object_id, float('-inf'))
        focused = age < self.ttl_s
        weight = 1.0 + (0.5 * (1 - age / self.ttl_s) if focused else 0)
        return {"weight": round(weight, 2), "focused": focused,
                "basis": "recent_conversation" if focused else "ordinary_speech",
                "identity_uncertain": speaker.status == "unknown", "overlap": overlap}
