from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from .port import AssemblyContext, AssemblyFragment, AssemblySpeaker, LoadResult

if TYPE_CHECKING:
    from jshi.longtermexperience import LongTermExperiencePort
    from jshi.memory import MemoryPort
    from jshi.identity import IdentityRepository
    from jshi.personalworld import PersonalWorldPort
    from jshi.recognition import ObjectProfileRepository
    from jshi.subject.repository import SubjectRepository
    from jshi.tool.service import ToolService


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
    """个人世界源（08）：每轮装真源，供 05 按风格重写。不再与活跃区 id 去重。"""

    name = "personal"
    status = "implemented"

    def __init__(self, personal_world: PersonalWorldPort) -> None:
        self._personal_world = personal_world

    def load(self, ctx: AssemblyContext) -> LoadResult:
        from jshi.personalworld.values import is_binding, is_boundary

        selected = tuple(self._personal_world.select(ctx.subject_id))
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
            for item in selected
        )
        return LoadResult(fragments=fragments, raw_items=selected)


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
        seen: set[str] = set()
        fragments: list[AssemblyFragment] = []
        object_id = ctx.speaker.object_id if ctx.speaker else None
        involved_ids = tuple(
            dict.fromkeys(
                oid for oid in (
                    object_id,
                    *((ctx.speaker.mentioned_object_ids) if ctx.speaker else ()),
                ) if oid
            )
        )
        if involved_ids:
            self._absorb(
                fragments,
                seen,
                self._memory.recall(
                    ctx.subject_id,
                    ctx.input_text,
                    object_ids=involved_ids,
                    interlocutor_object_id=object_id,
                    level=ctx.recall_level,
                    limit=ctx.recall_limit,
                ),
            )
        for query in ctx.extra_queries:
            text = (query or "").strip()
            if not text:
                continue
            self._absorb(
                fragments,
                seen,
                self._memory.recall(
                    ctx.subject_id,
                    text,
                    object_id=None,
                    level=ctx.recall_level,
                    limit=ctx.recall_limit,
                ),
            )
        return LoadResult(fragments=tuple(fragments))

    def _absorb(self, fragments, seen, recalled) -> None:
        for item in recalled:
            if item.event_id in seen:
                continue
            seen.add(item.event_id)
            fragments.append(
                AssemblyFragment(
                    source="memory",
                    id=f"memory:{item.event_id}",
                    # 用 content（按 summary_level 选的摘要）装配；进程内无摘要则落回原文。
                    content=item.content or item.text,
                    object_id=item.object_id,
                    query_object_role=item.query_object_role,
                    kind=item.kind or "fact",
                    status="active",
                    importance=0.5,
                    source_ids=(item.event_id, *item.source_ids),
                    always=False,
                    occurred_at=item.occurred_at,
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


class PersonExperienceSource:
    """按本轮已确认的对话对象读取可修订的长期相处经验。"""

    name = "person_experience"
    status = "implemented"

    def __init__(
        self, experience: LongTermExperiencePort, *, budget_chars: int = 500
    ) -> None:
        self._experience = experience
        self._budget_chars = max(0, budget_chars)

    def load(self, ctx: AssemblyContext) -> LoadResult:
        speaker = ctx.speaker
        if (
            speaker is None
            or not speaker.object_id
            or speaker.status != "confirmed"
            or self._budget_chars == 0
        ):
            return LoadResult()
        hits = self._experience.recall_experience(
            ctx.subject_id,
            ctx.input_text,
            object_ids=(speaker.object_id,),
            budget_chars=self._budget_chars,
        )
        fragments = tuple(
            AssemblyFragment(
                source=self.name,
                id=f"person-experience:{item.object_id}",
                content=item.content,
                kind=item.type,
                object_id=item.object_id,
                query_object_role="interlocutor",
                status="active",
                importance=0.7,
                source_ids=item.source_event_ids,
                always=False,
                occurred_at=item.updated_at,
                variant=item.variant,
            )
            for item in hits
            if item.object_id == speaker.object_id and item.content.strip()
        )
        return LoadResult(fragments=fragments)


class PersonPortraitSource:
    """仅为已确认的当前说话人装载描述性肖像。"""

    name = "person_portrait"
    status = "implemented"

    def __init__(self, memory: object, *, budget_chars: int = 350) -> None:
        self._memory = memory
        self._budget_chars = max(0, budget_chars)

    def load(self, ctx: AssemblyContext) -> LoadResult:
        speaker = ctx.speaker
        if (
            speaker is None
            or not speaker.object_id
            or speaker.status != "confirmed"
            or self._budget_chars == 0
        ):
            return LoadResult()
        read = getattr(self._memory, "portrait", None)
        if not callable(read):
            return LoadResult()
        portrait = read(ctx.subject_id, speaker.object_id)
        if not isinstance(portrait, dict):
            return LoadResult()
        if (
            portrait.get("subject_id") != ctx.subject_id
            or portrait.get("object_id") != speaker.object_id
        ):
            return LoadResult()
        levels = portrait.get("levels") or {}
        texts: list[str] = []
        if isinstance(levels, dict):
            texts = [
                value.strip()
                for value in levels.values()
                if isinstance(value, str) and value.strip()
            ]
        if not texts:
            text = str(portrait.get("visible_summary") or "").strip()
        else:
            fitting = [item for item in texts if len(item) <= self._budget_chars]
            text = max(fitting, key=len) if fitting else min(texts, key=len)
        if not text:
            return LoadResult()
        if len(text) > self._budget_chars:
            text = text[: self._budget_chars - 1].rstrip() + "…"
        updated_at = portrait.get("updated_at")
        return LoadResult(
            fragments=(
                AssemblyFragment(
                    source=self.name,
                    id=f"person-portrait:{speaker.object_id}",
                    content=text,
                    kind="portrait",
                    object_id=speaker.object_id,
                    query_object_role="interlocutor",
                    status="active",
                    importance=0.65,
                    occurred_at=updated_at if isinstance(updated_at, datetime) else None,
                ),
            )
        )


class ToolSource:
    """200 工具源：按本轮话题装载账本条目，供组装报告追溯。"""

    name = "tool"
    status = "implemented"

    def __init__(self, service: ToolService) -> None:
        self._service = service

    def load(self, ctx: AssemblyContext) -> LoadResult:
        speaker = ctx.speaker
        if speaker is None or not speaker.object_id:
            return LoadResult()
        items = self._service.list_tool_related_entries(
            ctx.subject_id, speaker.object_id, query=ctx.input_text
        )
        fragments = tuple(
            AssemblyFragment(
                source="tool",
                id=f"tool:{item['id']}",
                content=str(item.get("new_info") or item.get("final_result") or item.get("status") or ""),
                kind=str(item.get("section") or ""),
                object_id=speaker.object_id,
                status="active",
                importance=0.6,
                source_ids=(str(item["id"]),),
                always=False,
            )
            for item in items
        )
        return LoadResult(fragments=fragments)
