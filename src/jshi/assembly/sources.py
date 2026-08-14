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
    """记忆源（09）占位：对象过滤 + 近因优先 + 低档截断。

    09 未实现原生回忆档位与对象过滤，本适配器直读事实历史占位；
    09 接入后改为透传，并把 status 更新为 implemented。
    """

    name = "memory"
    status = "implemented"

    def __init__(
        self,
        repository: SubjectRepository,
        memory: MemoryPort | None = None,
    ) -> None:
        self._repository = repository
        self._memory = memory

    def load(self, ctx: AssemblyContext) -> LoadResult:
        if not ctx.object_id:
            return LoadResult()
        if ctx.recall_level <= 3:
            limit = 3
        elif ctx.recall_level <= 6:
            limit = 5
        else:
            limit = 8
        if self._memory is not None:
            recalled = self._memory.recall(
                ctx.subject_id,
                ctx.input_text,
                limit=limit,
                object_id=ctx.object_id,
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
        records = self._repository.list_history(
            ctx.subject_id, limit=max(limit * 4, 64)
        )
        related = [
            record
            for record in records
            if record.kind.value == "fact"
            and str(record.content.get("object_id", "")) == ctx.object_id
        ]
        tokens = [token for token in ctx.input_text.lower().split() if token]

        def score(record) -> tuple[int, object]:
            text = str(record.content.get("text", "")).lower()
            hits = sum(1 for token in tokens if token in text) if tokens else 0
            return (hits, record.created_at)

        chosen = sorted(related, key=score)[-limit:]
        fragments = tuple(
            AssemblyFragment(
                source="memory",
                id=record.id,
                content=str(record.content.get("text", "")),
                kind="fact",
                status="active",
                importance=0.5,
                source_ids=(record.id,),
                always=False,
            )
            for record in chosen
        )
        return LoadResult(fragments=fragments)


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
