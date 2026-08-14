from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Mapping, Protocol, Sequence

if TYPE_CHECKING:
    from jshi.memory import RecalledFragment
    from jshi.subject.domain import PersonalItem
    from jshi.subject.repository import SubjectRepository


# 设计语义常量：活跃区 ≈ 模型上下文 × ratio（软约束），换算由 14 调参系统负责。
ACTIVE_ZONE_RATIO = 1 / 66
# 占位条数预算：未来按模型上下文 × ratio 换算，本期不按 token 核算。
ACTIVE_ZONE_DEFAULT_BUDGET = 8
# 软约束弹性：允许超出预算 1 条不剔除。
ACTIVE_ZONE_EVICTION_ELASTICITY = 1

_EVICTED = "evicted"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ActiveZoneEvent:
    """活跃区中的一条事件视图。"""

    event_id: str
    content: str
    status: str  # unfinished | completed
    updated_at: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)
    source_ids: tuple[str, ...] = ()

    @classmethod
    def from_personal_item(cls, item: PersonalItem) -> "ActiveZoneEvent":
        status = (
            "unfinished" if item.status.value == "active" else "completed"
        )
        return cls(
            event_id=item.id,
            content=item.content,
            status=status,
            updated_at=item.updated_at,
            metadata=dict(item.metadata),
            source_ids=item.source_ids,
        )


@dataclass(frozen=True)
class ActiveZoneView:
    """阶段② 装载结果：预算内的事件视图（不调整、不落库）。"""

    events: tuple[ActiveZoneEvent, ...]
    budget: int
    loaded_at: datetime = field(default_factory=utc_now)


class ActiveZonePort(Protocol):
    """活跃区系统（可替换接口）。

    程序侧窄而硬：按 id 维护事件视图、预算核算、剔除/落位、按 id 召回；
    不做关键词匹配，不调用模型。模型在认知阶段负责事件聚焦与筛选。
    """

    def load(
        self,
        subject_id: str,
        input_text: str,
        *,
        budget: int | None = None,
    ) -> ActiveZoneView:
        """只读装载活跃区事件视图；空集合法（首次输入不设初始化分支）。"""

    def evict_overflow(
        self,
        subject_id: str,
        view: ActiveZoneView,
        *,
        elasticity: int = ACTIVE_ZONE_EVICTION_ELASTICITY,
    ) -> tuple[ActiveZoneEvent, ...]:
        """超预算剔除（占位按 updated_at，完成事件优先），返回被剔除事件。"""

    def recall_by_ids(
        self, subject_id: str, event_ids: Sequence[str]
    ) -> tuple[RecalledFragment, ...]:
        """显式事件 id 直接装载/召回；恢复被剔除事件的活跃区资格。"""


class InProcessActiveZone:
    """最小实现：事件本体存 personal_items（kind=concern），活跃区为派生视图。

    剔除只写 metadata（zone_state=evicted），不删事件本体、不改事件状态；
    被剔除事件保留在个人世界中，可经 recall_by_ids 恢复并召回。
    """

    name = "in-process-active-zone"

    def __init__(
        self,
        repository: SubjectRepository,
        *,
        default_budget: int = ACTIVE_ZONE_DEFAULT_BUDGET,
    ) -> None:
        self._repository = repository
        self._default_budget = default_budget

    def load(
        self,
        subject_id: str,
        input_text: str,
        *,
        budget: int | None = None,
    ) -> ActiveZoneView:
        budget = budget if budget is not None else self._default_budget
        ordered = self._candidates(subject_id)
        events = tuple(
            ActiveZoneEvent.from_personal_item(item)
            for item in ordered[:budget]
        )
        return ActiveZoneView(events=events, budget=budget)

    def evict_overflow(
        self,
        subject_id: str,
        view: ActiveZoneView,
        *,
        elasticity: int = ACTIVE_ZONE_EVICTION_ELASTICITY,
    ) -> tuple[ActiveZoneEvent, ...]:
        candidates = self._candidates(subject_id)
        if len(candidates) <= view.budget + elasticity:
            return ()
        excess = len(candidates) - (view.budget + elasticity)
        # 剔除顺序：完成事件（旧→新）优先，未完成事件（旧→新）其次；
        # 保证未完成事件尽量留在活跃区。
        completed = sorted(
            (
                item
                for item in candidates
                if item.status.value != "active"
            ),
            key=lambda item: item.updated_at,
        )
        unfinished = sorted(
            (
                item
                for item in candidates
                if item.status.value == "active"
            ),
            key=lambda item: item.updated_at,
        )
        chosen = (completed + unfinished)[:excess]
        now = utc_now().isoformat()
        for item in chosen:
            self._repository.update_personal_metadata(
                item.id,
                {"zone_state": _EVICTED, "evicted_at": now},
            )
        return tuple(ActiveZoneEvent.from_personal_item(item) for item in chosen)

    def recall_by_ids(
        self, subject_id: str, event_ids: Sequence[str]
    ) -> tuple[RecalledFragment, ...]:
        # 延迟导入避免包初始化循环（activezone 保持运行时零依赖）。
        from jshi.memory import RecalledFragment

        fragments: list[RecalledFragment] = []
        for event_id in event_ids:
            try:
                item = self._repository.get_personal_item(event_id)
            except KeyError:
                continue
            if item.kind.value != "concern":
                continue
            if item.metadata.get("zone_state") == _EVICTED:
                self._repository.update_personal_metadata(
                    item.id,
                    {"zone_state": "active", "evicted_at": None},
                )
            fragments.append(
                RecalledFragment(
                    event_id=item.id,
                    event_type="event",
                    text=item.content,
                    kind="event",
                )
            )
        return tuple(fragments)

    def _candidates(self, subject_id: str) -> tuple[PersonalItem, ...]:
        items = self._repository.list_personal_items(
            subject_id,
            active_only=False,
        )
        in_zone = [
            item
            for item in items
            if item.kind.value == "concern"
            and item.metadata.get("zone_state") != _EVICTED
        ]
        unfinished = sorted(
            (
                item
                for item in in_zone
                if item.status.value == "active"
            ),
            key=lambda item: item.updated_at,
            reverse=True,
        )
        completed = sorted(
            (
                item
                for item in in_zone
                if item.status.value != "active"
            ),
            key=lambda item: item.updated_at,
            reverse=True,
        )
        return tuple(unfinished + completed)
