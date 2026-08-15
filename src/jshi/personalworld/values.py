from __future__ import annotations

from typing import Protocol, Sequence

from jshi.textutil import query_terms
from jshi.subject.domain import PersonalItem, PersonalKind
from jshi.subject.repository import SubjectRepository


def item_importance(item: PersonalItem) -> float:
    """个人条目的重要程度；暂存于 metadata，未来升格为正式字段。"""
    return float(item.metadata.get("importance", 1.0))


def is_boundary(item: PersonalItem) -> bool:
    """边界实例使用 metadata.role="boundary" 表达，暂不新增 PersonalKind。"""
    return item.metadata.get("role") == "boundary"


def is_binding(item: PersonalItem) -> bool:
    """binding 边界需要常驻约束；缺省视为 binding。"""
    return bool(item.metadata.get("binding", True))


class ValuesPort(Protocol):
    """100 价值观与边界的装载接口（本轮只做装载侧）。"""

    def select_values(
        self, subject_id: str, context: str, *, budget: int = 4
    ) -> Sequence[PersonalItem]: ...

    def select_boundaries(self, subject_id: str) -> Sequence[PersonalItem]:
        """返回不可越过边界，供行动前检查与常驻装载。"""
        ...

    def standing_constraints(self, subject_id: str) -> Sequence[PersonalItem]:
        """返回 binding 边界，供 08 汇总为常驻约束。"""
        ...


class InProcessValues:
    """100 装载侧最小实现：普通价值按重要程度、边界常驻。"""

    def __init__(self, repository: SubjectRepository) -> None:
        self._repository = repository

    def _active_values(self, subject_id: str) -> Sequence[PersonalItem]:
        return self._repository.list_personal_items(
            subject_id, kind=PersonalKind.VALUE, active_only=True
        )

    def select_values(
        self, subject_id: str, context: str, *, budget: int = 4
    ) -> Sequence[PersonalItem]:
        items = self._active_values(subject_id)
        terms = query_terms(context)
        ranked = sorted(
            (item for item in items if not is_boundary(item)),
            key=lambda item: _value_score(item, terms),
            reverse=True,
        )
        return tuple(ranked[: max(budget, 0)])

    def select_boundaries(self, subject_id: str) -> Sequence[PersonalItem]:
        items = self._active_values(subject_id)
        return tuple(
            item for item in items if is_boundary(item) and is_binding(item)
        )

    def standing_constraints(self, subject_id: str) -> Sequence[PersonalItem]:
        return self.select_boundaries(subject_id)


def _value_score(item: PersonalItem, terms: frozenset[str]) -> tuple[float, float]:
    """价值排序：重要程度优先，查询相关性只破同一重要程度内的并列。"""

    hay = item.content.lower()
    relevance = sum(1 for term in terms if term in hay) if terms else 0.0
    return (
        item_importance(item),
        relevance,
    )
