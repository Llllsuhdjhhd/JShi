from __future__ import annotations

from typing import Sequence

from jshi.recognition import CarrierEntry, ObjectProfile, ObjectProfileRepository

from .port import ObjectSystemPort


class InProcessObjectSystem:
    """01 对象系统的最小端口实现，只包装对象档案仓库。"""

    def __init__(self, profiles: ObjectProfileRepository) -> None:
        self._profiles = profiles

    def get(self, object_id: str) -> ObjectProfile | None:
        return self._profiles.get(object_id)

    def ensure_provisional(
        self,
        *,
        object_id: str,
        label: str,
        source: str,
        carriers: Sequence[CarrierEntry] = (),
    ) -> ObjectProfile:
        existing = self._profiles.get(object_id)
        if existing is not None:
            return existing
        return self._profiles.create(
            ObjectProfile(
                object_id=object_id,
                label=label,
                carriers=tuple(carriers),
                source=source,
                status="provisional",
            )
        )

    def confirm(self, object_id: str) -> ObjectProfile:
        return self._profiles.update_status(object_id, "confirmed")

    def deny(self, object_id: str) -> ObjectProfile:
        return self._profiles.update_status(object_id, "rejected")

    def link_carrier(self, object_id: str, carrier: CarrierEntry) -> ObjectProfile:
        current = self._profiles.get(object_id)
        if current is None:
            raise KeyError(object_id)
        carriers = tuple(dict.fromkeys((*current.carriers, carrier)))
        return self._profiles.create(
            ObjectProfile(
                object_id=current.object_id,
                label=current.label,
                aliases=current.aliases,
                carriers=carriers,
                channel=current.channel,
                source=current.source,
                status=current.status,
                created_at=current.created_at,
                updated_at=current.updated_at,
            )
        )

    def list_objects(self) -> tuple[ObjectProfile, ...]:
        return tuple(self._profiles.list())
