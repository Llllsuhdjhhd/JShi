from __future__ import annotations

from typing import TYPE_CHECKING

from .port import AssemblyContext, AssemblyFragment, AssemblySpeaker, LoadResult

if TYPE_CHECKING:
    from jshi.memory import MemoryPort
    from jshi.identity import IdentityRepository
    from jshi.personalworld import PersonalWorldPort
    from jshi.recognition import ObjectProfileRepository
    from jshi.subject.repository import SubjectRepository


def speaker_summary(speaker: AssemblySpeaker) -> str:
    alias_text = "、".join(speaker.aliases)
    return (
        f"名字={speaker.label}；称呼={alias_text}；"
        f"状态={speaker.status}；object_id={speaker.object_id}"
    )


def memory_display_text(
    text: str,
    *,
    label: str,
    aliases: tuple[str, ...],
) -> str:
    alias_text = "、".join(aliases)
    return f"{label}（{alias_text}）：{text}"


class IdentitySource:
    """主体身份源：匠石档案与叙事。不是 01。"""

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


class ObjectSource:
    """对象源（01）：消费已解析的 SpeakerCandidate，不再 resolve。"""

    name = "object"
    status = "implemented"

    def load(self, ctx: AssemblyContext) -> LoadResult:
        speaker = ctx.speaker
        if speaker is None or not speaker.object_id:
            return LoadResult()
        return LoadResult(
            fragments=(
                AssemblyFragment(
                    source="object",
                    id=f"object:{speaker.object_id}",
                    content=speaker_summary(speaker),
                    kind="speaker",
                    status=speaker.status,
                    importance=1.0,
                    source_ids=(speaker.object_id,),
                    always=True,
                ),
            )
        )


class ActivityWindowSource:
    """上一活跃区源（02）：只翻译已读入的 ContextViewState，不调用 load。"""

    name = "activity"
    status = "implemented"

    def load(self, ctx: AssemblyContext) -> LoadResult:
        view = ctx.context_view
        if view is None:
            return LoadResult()
        fragments: list[AssemblyFragment] = []
        text = view.context_text or ""
        if text:
            fragments.append(
                AssemblyFragment(
                    source="activity",
                    id=f"context-v{view.version}",
                    content=text,
                    kind="context_view",
                    status="active",
                    importance=1.0,
                    source_ids=tuple(view.segment_refs),
                    always=True,
                )
            )
        for ref, excerpt in view.recall_excerpts:
            fragments.append(
                AssemblyFragment(
                    source="activity",
                    id=ref,
                    content=excerpt,
                    kind="recall_excerpt",
                    status="active",
                    importance=1.0,
                    source_ids=(ref,),
                    always=False,
                )
            )
        return LoadResult(fragments=tuple(fragments))


class PersonalWorldSource:
    """个人世界源（08）：透传 select；与活跃区已有 personal id 去重。"""

    name = "personal"
    status = "implemented"

    def __init__(self, personal_world: PersonalWorldPort) -> None:
        self._personal_world = personal_world

    def load(self, ctx: AssemblyContext) -> LoadResult:
        from jshi.personalworld.values import is_binding, is_boundary

        occupied = _occupied_personal_ids(ctx.context_view)
        selected = tuple(self._personal_world.select(ctx.subject_id))
        skipped: list[str] = []
        kept = []
        for item in selected:
            fragment_id = f"personal:{item.id}"
            if fragment_id in occupied or item.id in occupied:
                skipped.append(fragment_id)
                continue
            kept.append(item)
        fragments = tuple(
            AssemblyFragment(
                source="personal",
                id=f"personal:{item.id}",
                content=item.content,
                kind="boundary" if is_boundary(item) else item.kind.value,
                status=item.status.value,
                importance=float(item.metadata.get("importance", 1.0)),
                source_ids=item.source_ids,
                always=item.kind.value == "commitment"
                or (is_boundary(item) and is_binding(item)),
            )
            for item in kept
        )
        return LoadResult(
            fragments=fragments,
            raw_items=tuple(kept),
            skipped_ids=tuple(skipped),
        )


def _occupied_personal_ids(view) -> set[str]:
    if view is None:
        return set()
    occupied: set[str] = set()
    occupied.update(view.segment_refs)
    occupied.update(view.focused_refs)
    occupied.update(ref for ref, _text in view.recall_excerpts)
    return occupied


class MemorySource:
    """记忆源（09）：透传 recall；展示名用 01 档案或当前说话人 join。"""

    name = "memory"
    status = "implemented"

    def __init__(
        self,
        repository: SubjectRepository,
        memory: MemoryPort | None = None,
        profiles: ObjectProfileRepository | None = None,
    ) -> None:
        from jshi.memory import InProcessHistoryMemory

        self._memory = memory or InProcessHistoryMemory(repository)
        self._profiles = profiles

    def load(self, ctx: AssemblyContext) -> LoadResult:
        object_id = ctx.speaker.object_id if ctx.speaker else None
        if not object_id:
            return LoadResult()
        recalled = self._memory.recall(
            ctx.subject_id,
            ctx.input_text,
            object_id=object_id,
            level=ctx.recall_level,
        )
        return LoadResult(
            fragments=tuple(
                AssemblyFragment(
                    source="memory",
                    id=f"memory:{item.event_id}",
                    content=self._with_names(item.text, item.object_id, ctx.speaker),
                    kind=item.kind or "fact",
                    status="active",
                    importance=0.5,
                    source_ids=(item.event_id, *item.source_ids),
                    always=False,
                )
                for item in recalled
            )
        )

    def _with_names(
        self,
        text: str,
        object_id: str | None,
        speaker: AssemblySpeaker | None,
    ) -> str:
        label = ""
        aliases: tuple[str, ...] = ()
        if object_id and self._profiles is not None:
            profile = self._profiles.get(object_id)
            if profile is not None:
                label = profile.label
                aliases = profile.aliases
        if not label and speaker is not None and speaker.object_id == object_id:
            label = speaker.label
            aliases = speaker.aliases
        if not label:
            return text
        return memory_display_text(text, label=label, aliases=aliases)
