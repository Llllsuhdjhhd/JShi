from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from jshi.activezone import ActiveZoneView
from jshi.attention import ChancePort, PlaceholderChance
from jshi.core import Provenance, SubjectState

from .port import (
    AssemblyContext,
    AssemblyFragment,
    AssemblySourcePort,
    SourceLoadReport,
)


@dataclass(frozen=True)
class AssembledWorkingSet:
    """组装器输出：统一片段 + 主体状态快照 + 活跃区视图 + 装载报告。"""

    input_text: str
    subject_state: SubjectState
    fragments: tuple[AssemblyFragment, ...]
    active_zone: ActiveZoneView | None = None
    active_event_ids: tuple[str, ...] = ()
    personal_items: tuple[object, ...] = ()
    report: tuple[SourceLoadReport, ...] = ()


class CurrentStateAssembler:
    """03 组装编排者：收集 → 去重 → 预算截断 → 快照 → 偶然性 → 报告。

    各源相互独立，接口可并行；占位实现顺序调用（本地读取开销小）。
    单个源失败只记录原因，不影响其余源。
    """

    name = "current-state-assembler"

    def __init__(
        self,
        sources: Sequence[AssemblySourcePort],
        chance: ChancePort | None = None,
    ) -> None:
        self.sources = tuple(sources)
        self.chance = chance or PlaceholderChance()

    def assemble(self, ctx: AssemblyContext) -> AssembledWorkingSet:
        collected: list[AssemblyFragment] = []
        raw_items: list[object] = []
        errors: dict[str, str] = {}
        for source in self.sources:
            try:
                result = source.load(ctx)
                collected.extend(result.fragments)
                raw_items.extend(result.raw_items)
            except Exception as exc:  # 失败隔离：单源失败不影响组装
                errors[source.name] = str(exc)

        # 去重：保留第一个出现的片段；源注册顺序即优先级
        # （事件先于个人世界，个人世界先于记忆）。
        deduped: dict[str, AssemblyFragment] = {}
        for fragment in collected:
            deduped.setdefault(fragment.id, fragment)
        fragments = tuple(deduped.values())

        # 预算截断：常驻与事件源不占组装额外预算（事件已由活跃区预算）；
        # 其余按 个人世界 > 记忆 > 其他 的注册顺序截断。
        kept: list[AssemblyFragment] = []
        skipped_by_source: dict[str, list[str]] = {}
        budget_used = 0
        for fragment in fragments:
            if fragment.always or fragment.source == "event":
                kept.append(fragment)
                continue
            if budget_used < ctx.budget_extra:
                kept.append(fragment)
                budget_used += 1
            else:
                skipped_by_source.setdefault(fragment.source, []).append(
                    fragment.id
                )
        kept_fragments = tuple(kept)

        reports = tuple(
            SourceLoadReport(
                source=source.name,
                status=source.status,
                loaded_ids=tuple(
                    fragment.id
                    for fragment in kept_fragments
                    if fragment.source == source.name
                ),
                skipped_ids=tuple(
                    skipped_by_source.get(source.name, ())
                ),
                budget=ctx.budget_extra
                if source.name in {"personal", "memory"}
                else 0,
                error=errors.get(source.name),
            )
            for source in self.sources
        )

        working_set = AssembledWorkingSet(
            input_text=ctx.input_text,
            subject_state=self._subject_state(ctx.subject_id, kept_fragments),
            fragments=kept_fragments,
            active_zone=ctx.active_zone,
            active_event_ids=tuple(
                fragment.id
                for fragment in kept_fragments
                if fragment.source == "event"
            ),
            personal_items=tuple(raw_items),
            report=reports,
        )
        return self.chance.apply("assemble", working_set)

    @staticmethod
    def _subject_state(
        subject_id: str,
        fragments: Sequence[AssemblyFragment],
    ) -> SubjectState:
        def first(source: str, kind: str) -> str:
            for fragment in fragments:
                if fragment.source == source and fragment.kind == kind:
                    return fragment.content
            return ""

        return SubjectState(
            subject_id=subject_id,
            identity_summary=first("identity", "identity_summary") or "匠石",
            current_stance=first("identity", "stance"),
            salient_values=tuple(
                fragment.content
                for fragment in fragments
                if fragment.source == "personal" and fragment.kind == "value"
            ),
            commitments=tuple(
                fragment.content
                for fragment in fragments
                if fragment.source == "personal"
                and fragment.kind == "commitment"
            ),
            concerns=tuple(
                fragment.content
                for fragment in fragments
                if fragment.source == "event"
                and fragment.status == "unfinished"
            ),
            provenance=Provenance(
                source="assembled_current_state",
                method="load_existing_only",
            ),
        )
