from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Sequence

from jshi.core import Event


class InnerActivityKind(StrEnum):
    REVIEW = "review"
    REFLECTION = "reflection"
    LEARNING = "learning"
    SIMULATION = "simulation"
    DREAM = "dream"


@dataclass(frozen=True)
class InnerActivity:
    kind: InnerActivityKind
    prompt: str
    source_event_ids: tuple[str, ...] = ()
    allow_imagination: bool = False


@dataclass
class InnerActivityScheduler:
    """Explicit scheduler placeholder; no background work is started implicitly."""

    queued: list[InnerActivity] = field(default_factory=list)

    def schedule(self, activity: InnerActivity) -> None:
        self.queued.append(activity)

    def next(self) -> InnerActivity | None:
        return self.queued.pop(0) if self.queued else None

    def propose_review(self, recent_events: Sequence[Event]) -> InnerActivity | None:
        if not recent_events:
            return None
        return InnerActivity(
            kind=InnerActivityKind.REVIEW,
            prompt="回顾这些近期经历，区分事实、解释与仍未解决的问题。",
            source_event_ids=tuple(event.id for event in recent_events),
        )
