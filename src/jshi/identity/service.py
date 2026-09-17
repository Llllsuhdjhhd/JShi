from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class IdentityProfile:
    subject_id: str
    name: str
    origin: str
    narrative: str = ""
    revision: int = 1


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
            profile = IdentityProfile(
                subject_id=item["subject_id"],
                name=item["name"],
                origin=item["origin"],
                narrative=item.get("narrative", ""),
                revision=item.get("revision", 1),
            )
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
