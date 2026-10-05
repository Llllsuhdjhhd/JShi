from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from uuid import uuid4
import re


def sentences(text: str) -> tuple[str, ...]:
    """Keep sentence order and punctuation; bound very long synthesis requests."""
    parts = re.findall(r"[^。！？!?\n]+[。！？!?\n]*", text.strip())
    return tuple(part[i:i + 180] for part in parts for i in range(0, len(part), 180) if part[i:i + 180].strip())


@dataclass
class Delivery:
    reply_id: str
    object_id: str
    texts: tuple[str, ...]
    state: str = "queued"
    completed: set[int] = field(default_factory=set)
    current: int | None = None
    generated: bool = False
    reason: str = ""
    targets: tuple[tuple[str, ...], ...] = ()

    def snapshot(self) -> dict:
        return {"reply_id": self.reply_id, "object_id": self.object_id,
                "state": self.state, "completed": sorted(self.completed),
                "played_text": "".join(t for i, t in enumerate(self.texts) if i in self.completed),
                "partial_text": self.texts[self.current] if self.current is not None and self.current not in self.completed else "",
                "pending_text": "".join(t for i, t in enumerate(self.texts) if i not in self.completed and i != self.current),
                "reason": self.reason, "targets": [list(t) for t in self.targets]}


class DeliveryTracker:
    """Only player acknowledgements can mark text as played."""
    def __init__(self) -> None:
        self.lock = RLock()
        self.current: Delivery | None = None
        self.previous: dict | None = None

    def begin(self, text: str, object_id: str, *, items=None) -> Delivery:
        with self.lock:
            if self.current:
                self.previous = self.current.snapshot()
            self.current = Delivery(uuid4().hex, object_id, sentences(text))
            if items is not None:
                texts, targets = [], []
                for item in items:
                    for segment in sentences(item.text):
                        texts.append(segment)
                        targets.append(item.target_ids)
                self.current.texts = tuple(texts)
                self.current.targets = tuple(targets)
            return self.current

    def accepts(self, reply_id: str) -> bool:
        with self.lock:
            return bool(self.current and self.current.reply_id == reply_id and self.current.state not in {"stopped", "failed", "completed"})

    def pause(self) -> bool:
        with self.lock:
            if not self.current or not self.accepts(self.current.reply_id) or self.current.state == "paused":
                return False
            self.current.state = "paused"
            return True

    def resume(self) -> bool:
        with self.lock:
            if not self.current or self.current.state != "paused":
                return False
            self.current.state = "playing"
            return True

    def stop(self, reason: str = "interrupted") -> str:
        with self.lock:
            if not self.current:
                return ""
            if self.current.state != "completed":
                self.current.state = "stopped"
                self.current.reason = reason
            return self.current.reply_id

    def acknowledge(self, reply_id: str, segment: int, phase: str) -> bool:
        with self.lock:
            if not self.accepts(reply_id) or not 0 <= segment < len(self.current.texts):
                return False
            d = self.current
            if phase == "started":
                d.current = segment
                if d.state != "paused":
                    d.state = "playing"
            elif phase == "completed":
                if segment != len(d.completed):
                    return False  # acknowledgements must be ordered, not arbitrary
                d.completed.add(segment)
                d.current = None
                if len(d.completed) == len(d.texts) and d.generated:
                    d.state = "completed"
            else:
                return False
            return True

    def generated(self, reply_id: str) -> None:
        with self.lock:
            if self.accepts(reply_id):
                self.current.generated = True
                if len(self.current.completed) == len(self.current.texts):
                    self.current.state = "completed"

    def fail(self, reply_id: str, reason: str) -> None:
        with self.lock:
            if self.accepts(reply_id):
                self.current.state = "failed"
                self.current.reason = reason

    def snapshot(self) -> dict:
        with self.lock:
            return self.current.snapshot() if self.current else {}

    def context_note(self) -> str:
        import json
        with self.lock:
            snapshots = [v for v in (self.previous, self.snapshot()) if v]
        if not snapshots:
            return ""
        return ("【实际语音交付】以播放器反馈为准；partial_text 只表示该句部分播放，"
                "不代表整句已听到；pending_text 尚未播放。先前场中准备说的内容不等于已说完。\n"
                + json.dumps(snapshots, ensure_ascii=False))
