from __future__ import annotations

from typing import TYPE_CHECKING

from .port import AssemblyContext, AssemblyFragment, LoadResult

if TYPE_CHECKING:
    from jshi.memory import MemoryPort
    from jshi.identity import IdentityRepository
    from jshi.personalworld import PersonalWorldPort
    from jshi.subject.repository import SubjectRepository


class IdentitySource:
    """身份源（01）：档案与叙事，常驻。"""

    name = "identity"
    status = "implemented"

    def __init__(self, identities: IdentityRepository) -> None:
        self._identities = identities

    def load(self, ctx: AssemblyContext) -> LoadResult:
        profile = self._identities.get(ctx.subject_id)
        summary = f"{profile.name}；来源：{profile.origin}"
        return LoadResult(
            fragments=(
                AssemblyFragment(
                    source="identity",
                    id=f"identity:{ctx.subject_id}",
                    content=summary,
                    kind="identity_summary",
                    status="active",
                    importance=1.0,
                    source_ids=(),
                    always=True,
                ),
                AssemblyFragment(
                    source="identity",
                    id=f"stance:{ctx.subject_id}",
                    content=profile.narrative,
                    kind="stance",
                    status="active",
                    importance=1.0,
                    source_ids=(),
                    always=True,
                ),
            )
        )


class EventSource:
    """事件源（07 视图来自 02）：未完成常驻，完成按活跃区预算。"""

    name = "event"
    status = "implemented"

    def load(self, ctx: AssemblyContext) -> LoadResult:
        view = ctx.active_zone
        if view is None:
            return LoadResult()
        fragments = tuple(
            AssemblyFragment(
                source="event",
                id=event.event_id,
                content=event.content,
                kind="event",
                status=event.status,
                importance=1.0,
                source_ids=event.source_ids,
                always=event.status == "unfinished",
            )
            for event in view.events
        )
        return LoadResult(fragments=fragments)


class PersonalWorldSource:
    """个人世界源（08）：承诺常驻；其余按重要程度与预算；跳过 concern。"""

    name = "personal"
    status = "implemented"

    def __init__(self, personal_world: PersonalWorldPort) -> None:
        self._personal_world = personal_world

    def load(self, ctx: AssemblyContext) -> LoadResult:
        selected = tuple(
            self._personal_world.select(
                ctx.subject_id,
                ctx.input_text,
                # 08 的预算语义是"总数（含常驻）"：这里给足候选，
                # 最终非常驻截断由 03 组装器统一控制。
                budget=max(ctx.budget_extra + 16, 20),
            )
        )
        raw = tuple(item for item in selected if item.kind.value != "concern")
        fragments = tuple(
            AssemblyFragment(
                source="personal",
                id=item.id,
                content=item.content,
                kind=item.kind.value,
                status=item.status.value,
                importance=float(item.metadata.get("importance", 1.0)),
                source_ids=item.source_ids,
                always=item.kind.value == "commitment",
            )
            for item in raw
        )
        return LoadResult(fragments=fragments, raw_items=raw)


class MemorySource:
    """记忆源（09）：透传记忆端口，对象过滤 + 近因优先 + 档位语义由 09 实现。"""

    name = "memory"
    status = "implemented"

    def __init__(
        self,
        repository: SubjectRepository,
        memory: MemoryPort | None = None,
    ) -> None:
        # 始终走 09 端口；未注入时使用最小进程内实现，避免 03 直读事实历史。
        from jshi.memory import InProcessHistoryMemory

        self._memory = memory or InProcessHistoryMemory(repository)

    def load(self, ctx: AssemblyContext) -> LoadResult:
        if not ctx.object_id:
            return LoadResult()
        recalled = self._memory.recall(
            ctx.subject_id,
            ctx.input_text,
            object_id=ctx.object_id,
            level=ctx.recall_level,
        )
        return LoadResult(
            fragments=tuple(
                AssemblyFragment(
                    source="memory",
                    id=item.event_id,
                    content=item.text,
                    kind=item.kind or "fact",
                    status="active",
                    importance=0.5,
                    source_ids=(item.event_id, *item.source_ids),
                    always=False,
                )
                for item in recalled
            )
        )


class EpistemicSource:
    """认识状态源（06）占位：未实现，报告标记 placeholder。"""

    name = "epistemic"
    status = "placeholder"

    def load(self, ctx: AssemblyContext) -> LoadResult:
        return LoadResult()


class ObjectSource:
    """对象档案摘要源（01）占位：未实现，报告标记 placeholder。"""

    name = "object"
    status = "placeholder"

    def load(self, ctx: AssemblyContext) -> LoadResult:
        return LoadResult()
