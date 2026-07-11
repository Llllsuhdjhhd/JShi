from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from jshi.core import CandidateChange, Event, Provenance, SubjectState


@dataclass(frozen=True)
class IdentityProfile:
    subject_id: str
    name: str
    origin: str
    narrative: str = ""
    commitments: tuple[str, ...] = ()
    revision: int = 1
    accepted_change_ids: tuple[str, ...] = ()


class IdentityRepository:
    """Small JSON-backed repository; the interface remains replaceable."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._profiles: dict[str, IdentityProfile] = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for item in raw:
            item["commitments"] = tuple(item.get("commitments", ()))
            item["accepted_change_ids"] = tuple(item.get("accepted_change_ids", ()))
            profile = IdentityProfile(**item)
            self._profiles[profile.subject_id] = profile

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                [asdict(profile) for profile in self._profiles.values()],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def create(self, profile: IdentityProfile) -> None:
        if profile.subject_id in self._profiles:
            raise ValueError(f"Identity already exists: {profile.subject_id}")
        self._profiles[profile.subject_id] = profile
        self._save()

    def get(self, subject_id: str) -> IdentityProfile:
        return self._profiles[subject_id]

    def accept(self, change: CandidateChange, minimum_significance: float = 0.8) -> bool:
        if change.target != "identity" or change.significance < minimum_significance:
            return False
        profile = self.get(change.subject_id)
        narrative = str(change.value.get("narrative", profile.narrative))
        self._profiles[change.subject_id] = replace(
            profile,
            narrative=narrative,
            revision=profile.revision + 1,
            accepted_change_ids=profile.accepted_change_ids + (change.id,),
        )
        self._save()
        return True


class SelfPort(Protocol):
    def synthesize(
        self,
        identity: IdentityProfile,
        event: Event,
        contributions: Sequence[Mapping[str, Any]],
    ) -> SubjectState: ...


class SimpleSelfPort:
    """Traceable placeholder for a future subject-integration mechanism."""

    def synthesize(
        self,
        identity: IdentityProfile,
        event: Event,
        contributions: Sequence[Mapping[str, Any]],
    ) -> SubjectState:
        values: list[str] = []
        concerns: list[str] = []
        uncertainties: list[str] = []
        for contribution in contributions:
            values.extend(map(str, contribution.get("values", ())))
            concerns.extend(map(str, contribution.get("concerns", ())))
            uncertainties.extend(map(str, contribution.get("uncertainties", ())))

        stance = identity.narrative or f"我是{identity.name}，正在面对当前事件。"
        return SubjectState(
            subject_id=identity.subject_id,
            identity_summary=f"{identity.name}；来源：{identity.origin}",
            current_stance=stance,
            salient_values=tuple(dict.fromkeys(values)),
            commitments=identity.commitments,
            concerns=tuple(dict.fromkeys(concerns)),
            uncertainties=tuple(dict.fromkeys(uncertainties)),
            provenance=Provenance(
                source="self_port",
                source_event_ids=(event.id,),
                method=self.__class__.__name__,
            ),
        )
