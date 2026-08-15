from __future__ import annotations

from typing import Protocol, Sequence

from jshi.textutil import query_terms
from jshi.subject.domain import PersonalItem, PersonalKind, PersonalStatus
from jshi.subject.repository import SubjectRepository

from .values import (
    InProcessValues,
    ValuesPort,
    is_loadable_value,
    item_importance,
)


class PersonalWorldPort(Protocol):
    """可替换的个人世界装载接口（03 统一入口）。"""

    def select(
        self, subject_id: str, query: str, *, budget: int = 20
    ) -> Sequence[PersonalItem]: ...

    def standing_constraints(self, subject_id: str) -> Sequence[PersonalItem]:
        """承诺、主体面未完成现实与 binding 边界（常驻约束清单）。"""
        ...

    def select_boundaries(self, subject_id: str) -> Sequence[PersonalItem]:
        """不可越过边界，供行动前检查与常驻装载。"""
        ...


class InProcessPersonalWorld:
    """08 薄壳：协调遗留约束与 100 价值观/边界装载。"""

    _constraint_kinds = frozenset({PersonalKind.COMMITMENT, PersonalKind.CONCERN})

    def __init__(
        self,
        repository: SubjectRepository,
        values: ValuesPort | None = None,
    ) -> None:
        self._repository = repository
        self._values = values or InProcessValues(repository)

    def _legacy_constraints(self, subject_id: str) -> Sequence[PersonalItem]:
        """承诺与主体面未完成现实的过渡态路径，未来移交 101 / 07。"""
        items = self._repository.list_personal_items(subject_id, active_only=True)
        return tuple(
            item for item in items if item.kind in self._constraint_kinds
        )

    def select(
        self, subject_id: str, query: str, *, budget: int = 20
    ) -> Sequence[PersonalItem]:
        items = self._repository.list_personal_items(subject_id, active_only=False)
        constraints = tuple(
            item
            for item in items
            if item.kind in self._constraint_kinds
            and item.status is PersonalStatus.ACTIVE
        )
        boundaries = tuple(self._values.select_boundaries(subject_id))
        boundary_ids = {item.id for item in boundaries}
        terms = query_terms(query)
        ranked = sorted(
            (
                item
                for item in items
                if item.kind not in self._constraint_kinds
                and item.id not in boundary_ids
                and _ordinary_loadable(item)
            ),
            key=lambda item: _selection_score(item, terms),
            reverse=True,
        )
        return constraints + boundaries + tuple(ranked[: max(budget, 0)])

    def standing_constraints(self, subject_id: str) -> Sequence[PersonalItem]:
        legacy = self._legacy_constraints(subject_id)
        boundaries = tuple(self._values.standing_constraints(subject_id))
        return legacy + boundaries

    def select_boundaries(self, subject_id: str) -> Sequence[PersonalItem]:
        return tuple(self._values.select_boundaries(subject_id))


def _selection_score(item: PersonalItem, terms: frozenset[str]) -> tuple[float, float]:
    """普通条目排序：重要程度优先，查询相关性只破同一重要程度内的并列。"""

    hay = item.content.lower()
    relevance = sum(1 for term in terms if term in hay) if terms else 0.0
    return (item_importance(item), relevance)


def _ordinary_loadable(item: PersonalItem) -> bool:
    """非约束普通条目是否可装载；价值走 100 状态，其余走 active。"""
    if item.kind is PersonalKind.VALUE:
        return is_loadable_value(item)
    return item.status is PersonalStatus.ACTIVE
