from __future__ import annotations

from typing import Protocol, Sequence

from jshi.subject.domain import PersonalItem, PersonalKind, PersonalStatus
from jshi.subject.repository import SubjectRepository

from .values import InProcessValues, ValuesPort, item_importance

ITEM_LEVELS = ("低", "中", "高")
DEFAULT_LEVEL = "中"
LOAD_QUOTAS = {
    "低": {"高": 2, "中": 1, "低": 0},
    "中": {"高": 8, "中": 8, "低": 2},
    "高": {"高": 16, "中": 12, "低": 4},
}
_REMAINDER_KINDS = frozenset(
    {
        PersonalKind.RELATIONSHIP,
        PersonalKind.CAPABILITY,
        PersonalKind.AESTHETIC,
        PersonalKind.SELF_UNDERSTANDING,
    }
)


class PersonalWorldModule(Protocol):
    name: str

    def list_standing(self, subject_id: str) -> Sequence[PersonalItem]: ...

    def list_ordered(self, subject_id: str) -> Sequence[PersonalItem]: ...


class PersonalWorldPort(Protocol):
    """08 薄壳：合并子模块有序列表，按 id 去重，按等级取量。"""

    def select(
        self,
        subject_id: str,
        query: str = "",
        *,
        load_level: str = DEFAULT_LEVEL,
    ) -> Sequence[PersonalItem]: ...

    def standing_constraints(self, subject_id: str) -> Sequence[PersonalItem]: ...

    def select_boundaries(self, subject_id: str) -> Sequence[PersonalItem]: ...


class InProcessPersonalWorld:
    """08：不排序、不按 query 筛选；concern 丢弃。"""

    def __init__(
        self,
        repository: SubjectRepository,
        values: ValuesPort | None = None,
        modules: Sequence[PersonalWorldModule] | None = None,
    ) -> None:
        self._repository = repository
        self._values = values or InProcessValues(repository)
        self._modules = tuple(modules) if modules is not None else (
            ValuesModule(self._values),
            CommitmentModule(repository),
            RemainderModule(repository),
        )

    def select(
        self,
        subject_id: str,
        query: str = "",
        *,
        load_level: str = DEFAULT_LEVEL,
    ) -> Sequence[PersonalItem]:
        del query
        standing = self.standing_constraints(subject_id)
        seen = {item.id for item in standing}
        ordinary: list[PersonalItem] = []
        for module in self._modules:
            for item in module.list_ordered(subject_id):
                if item.kind is PersonalKind.CONCERN or item.id in seen:
                    continue
                seen.add(item.id)
                ordinary.append(item)
        return standing + _take_by_level(ordinary, load_level)

    def standing_constraints(self, subject_id: str) -> Sequence[PersonalItem]:
        seen: set[str] = set()
        items: list[PersonalItem] = []
        for module in self._modules:
            for item in module.list_standing(subject_id):
                if item.kind is PersonalKind.CONCERN or item.id in seen:
                    continue
                seen.add(item.id)
                items.append(item)
        return tuple(items)

    def select_boundaries(self, subject_id: str) -> Sequence[PersonalItem]:
        return tuple(self._values.select_boundaries(subject_id))


class ValuesModule:
    name = "values"

    def __init__(self, values: ValuesPort) -> None:
        self._values = values

    def list_standing(self, subject_id: str) -> Sequence[PersonalItem]:
        return tuple(self._values.standing_constraints(subject_id))

    def list_ordered(self, subject_id: str) -> Sequence[PersonalItem]:
        return tuple(self._values.list_ordered(subject_id))


class CommitmentModule:
    """101 未展开前的承诺占位：只交已有承诺，排序在本适配器内。"""

    name = "commitment"

    def __init__(self, repository: SubjectRepository) -> None:
        self._repository = repository

    def list_standing(self, subject_id: str) -> Sequence[PersonalItem]:
        return self.list_ordered(subject_id)

    def list_ordered(self, subject_id: str) -> Sequence[PersonalItem]:
        items = [
            item
            for item in self._repository.list_personal_items(
                subject_id, kind=PersonalKind.COMMITMENT, active_only=True
            )
            if item.status is PersonalStatus.ACTIVE
        ]
        items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(items)


class RemainderModule:
    """102–104 未展开前的占位：只交已有条目，按重要程度排序。"""

    name = "remainder"

    def __init__(self, repository: SubjectRepository) -> None:
        self._repository = repository

    def list_standing(self, subject_id: str) -> Sequence[PersonalItem]:
        del subject_id
        return ()

    def list_ordered(self, subject_id: str) -> Sequence[PersonalItem]:
        items = [
            item
            for item in self._repository.list_personal_items(
                subject_id, active_only=True
            )
            if item.kind in _REMAINDER_KINDS
        ]
        items.sort(
            key=lambda item: (item_importance(item), item.id),
            reverse=True,
        )
        return tuple(items)


def normalize_level(level: str) -> str:
    return level if level in ITEM_LEVELS else DEFAULT_LEVEL


def _take_by_level(
    items: Sequence[PersonalItem], load_level: str
) -> tuple[PersonalItem, ...]:
    quotas = dict(LOAD_QUOTAS[normalize_level(load_level)])
    taken: list[PersonalItem] = []
    for item in items:
        band = normalize_level(item.level)
        remaining = quotas.get(band, 0)
        if remaining <= 0:
            continue
        taken.append(item)
        quotas[band] = remaining - 1
    return tuple(taken)
