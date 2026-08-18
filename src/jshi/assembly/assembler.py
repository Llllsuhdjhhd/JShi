from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from jshi.attention import ChancePort, PlaceholderChance
from jshi.core import Provenance, SubjectState
from jshi.experienceledger import ContextViewState

from .port import (
    AssemblyContext,
    AssemblyFragment,
    AssemblySourcePort,
    AssemblySpeaker,
    SourceLoadReport,
)

_PROTECTED_SOURCES = frozenset({"identity", "object", "activity"})


@dataclass(frozen=True)
class AssembledWorkingSet:
    """组装器输出：刺激、说话人、快照、分片、既往视图、装载报告。"""

    input_text: str
    subject_state: SubjectState
    fragments: tuple[AssemblyFragment, ...]
    context_view: ContextViewState | None = None
    speaker: AssemblySpeaker | None = None
    personal_items: tuple[object, ...] = ()
    report: tuple[SourceLoadReport, ...] = ()


class CurrentStateAssembler:
    """03 组装编排者：收集 → 源内去重 → 全局上限（占位）→ 快照 → 报告。

    各源相互独立；占位实现顺序调用。单源失败只记录原因，不影响其余源。
    08 / 09 的筛选不在此复制；不得裁掉身份、对象、08 常驻与既往视图正文。
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
        personal_raw: list[object] = []
        errors: dict[str, str] = {}
        skipped_by_source: dict[str, list[str]] = {}
        for source in self.sources:
            try:
                result = source.load(ctx)
                collected.extend(result.fragments)
                if source.name == "personal":
                    personal_raw.extend(result.raw_items)
                skipped_by_source[source.name] = list(result.skipped_ids)
            except Exception as exc:  # 失败隔离：单源失败不影响组装
                errors[source.name] = str(exc)
                skipped_by_source.setdefault(source.name, [])

        deduped: dict[tuple[str, str], AssemblyFragment] = {}
        for fragment in collected:
            deduped.setdefault((fragment.source, fragment.id), fragment)
        fragments = tuple(deduped.values())

        kept: list[AssemblyFragment] = []
        budget_used = 0
        limit = ctx.working_set_limit
        for fragment in fragments:
            protected = fragment.always or fragment.source in _PROTECTED_SOURCES
            if protected:
                kept.append(fragment)
                continue
            if limit is None or budget_used < limit:
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
                skipped_ids=tuple(skipped_by_source.get(source.name, ())),
                budget=limit or 0,
                error=errors.get(source.name),
            )
            for source in self.sources
        )

        working_set = AssembledWorkingSet(
            input_text=ctx.input_text,
            speaker=ctx.speaker,
            subject_state=self._subject_state(ctx.subject_id, kept_fragments),
            fragments=kept_fragments,
            context_view=ctx.context_view,
            personal_items=tuple(personal_raw),
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
            concerns=(),
            provenance=Provenance(
                source="assembled_current_state",
                method="load_existing_only",
            ),
        )
