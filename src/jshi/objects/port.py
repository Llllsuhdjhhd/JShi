from __future__ import annotations

from typing import Protocol, Sequence

from jshi.recognition import CarrierEntry, ObjectProfile


class ObjectSystemPort(Protocol):
    def get(self, object_id: str) -> ObjectProfile | None: ...

    def ensure_provisional(
        self,
        *,
        object_id: str,
        label: str,
        source: str,
        carriers: Sequence[CarrierEntry] = (),
    ) -> ObjectProfile: ...

    def confirm(self, object_id: str) -> ObjectProfile: ...

    def deny(self, object_id: str) -> ObjectProfile: ...

    def link_carrier(self, object_id: str, carrier: CarrierEntry) -> ObjectProfile: ...

    def list_objects(self) -> tuple[ObjectProfile, ...]: ...
