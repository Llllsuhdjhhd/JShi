from __future__ import annotations

from typing import Protocol, Sequence

from jshi.subject.domain import PersonalItem, PersonalKind
from jshi.subject.repository import SubjectRepository


def item_importance(item: PersonalItem) -> float:
    """个人条目的重要程度；暂存于 metadata，未来升格为正式字段。"""
    return float(item.metadata.get("importance", 1.0))


class PersonalWorldPort(Protocol):
    """可替换的个人世界装载接口（与记忆系统平行）。

    负责在每次活动中选择哪些个人世界条目进入当前状态：
    承诺与主体面未完成现实作为约束始终装载；
    其余条目（价值、审美、能力、关系、自我理解等）按重要程度排序、
    在预算内截断。外部引擎可通过实现同一接口接入。
    """

    def select(
        self, subject_id: str, query: str, *, budget: int = 20
    ) -> Sequence[PersonalItem]: ...

    def standing_constraints(self, subject_id: str) -> Sequence[PersonalItem]:
        """承诺与主体面未完成现实（常驻约束清单），供归属判断与始终装载。"""
        ...


class InProcessPersonalWorld:
    """最小实现：进程内按重要程度排序、预算内截断。"""

    _constraint_kinds = frozenset({PersonalKind.COMMITMENT, PersonalKind.CONCERN})

    def __init__(self, repository: SubjectRepository) -> None:
        self._repository = repository

    def select(
        self, subject_id: str, query: str, *, budget: int = 20
    ) -> Sequence[PersonalItem]:
        items = self._repository.list_personal_items(subject_id, active_only=True)
        constraints = [
            item for item in items if item.kind in self._constraint_kinds
        ]
        ranked = sorted(
            (item for item in items if item.kind not in self._constraint_kinds),
            key=item_importance,
            reverse=True,
        )
        remain = max(budget - len(constraints), 0)
        return tuple(constraints + ranked[:remain])

    def standing_constraints(self, subject_id: str) -> Sequence[PersonalItem]:
        items = self._repository.list_personal_items(subject_id, active_only=True)
        return tuple(
            item for item in items if item.kind in self._constraint_kinds
        )
