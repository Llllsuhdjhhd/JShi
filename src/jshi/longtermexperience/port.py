"""长期经验的独立端口；与活动经历账本和 Event 回忆分开。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class PersonExperience:
    object_id: str
    content: str
    variant: str
    source_event_ids: tuple[str, ...]
    updated_at: datetime | None = None
    score: float = 0.0
    type: str = "person_experience"


@dataclass(frozen=True)
class PersonExperienceRefresh:
    object_id: str
    imported_white_paintings: int
    updated: bool
    pending_white_paintings: int
    pending_chars: int
    review_outcome: str


class LongTermExperiencePort(Protocol):
    def recall_experience(
        self,
        subject_id: str,
        query: str,
        *,
        object_ids: tuple[str, ...],
        budget_chars: int = 500,
    ) -> tuple[PersonExperience, ...]: ...

    def refresh_person_experience(
        self, subject_id: str, object_id: str
    ) -> PersonExperienceRefresh: ...
