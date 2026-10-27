from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence
from time import perf_counter

from jshi.attention import ChancePort, PlaceholderChance
from jshi.core import Provenance, SubjectState, params
from jshi.experienceledger import ContextViewState
from jshi.memory.contracts import RecallCandidateBatch

from .port import (
    AssemblyContext,
    AssemblyFragment,
    AssemblySourcePort,
    AssemblySpeaker,
    SourceLoadReport,
)

_PROTECTED_SOURCES = frozenset({"identity", "object"})


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
    tool_input: str = ""
    recall_batch: RecallCandidateBatch | None = None


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
        timings = {}
        recall_batch = None
        reasons: dict[str, list[tuple[str, str]]] = {}
        for source in self.sources:
            started = perf_counter()
            try:
                result = source.load(ctx)
                collected.extend(result.fragments)
                if source.name == "personal":
                    personal_raw.extend(result.raw_items)
                skipped_by_source[source.name] = list(result.skipped_ids)
                if result.recall_batch is not None:
                    recall_batch = result.recall_batch
            except Exception as exc:  # 失败隔离：单源失败不影响组装
                errors[source.name] = str(exc)
                skipped_by_source.setdefault(source.name, [])
            finally:
                timings[source.name] = round((perf_counter() - started) * 1000, 3)

        deduped: dict[tuple[str, str], AssemblyFragment] = {}
        for fragment in collected:
            key = ('recall', fragment.id) if fragment.source == 'memory' or fragment.kind == 'recall_excerpt' else (fragment.source, fragment.id)
            if key in deduped:
                skipped_by_source.setdefault(fragment.source, []).append(fragment.id)
                reasons.setdefault(fragment.source, []).append((fragment.id, 'duplicate'))
            else:
                deduped[key] = fragment
        fragments = tuple(deduped.values())

        # 魔法书默认：非保护片段 1500 字（回忆 + 普通价值）。缺省即生效，仍可显式覆盖。
        limit = (
            ctx.working_set_limit
            if ctx.working_set_limit is not None
            else params.working_set_limit()
        )

        # 分档（三档）：
        #  - protected：永不裁、不占预算 —— 身份 / 对象 / 既往视图正文 / 08 常驻(always)。
        #  - person_experience / person_portrait / memory：共享回忆预算，先分配人物认识，再分配事件；
        #    两者都可能被预算裁掉，不变成常驻人物事实。
        #  - ordinary：普通条目（个人世界普通价值等）—— 最低档，预算不足时先裁。
        #  - tool：不进工作集分片，原样并成 tool_input（03 不改写 200 的正文）。
        #
        # 先按档位收集，再按"memory → ordinary"顺序分配预算；被裁的计入 skipped。
        protected_frags: list[AssemblyFragment] = []
        memory_frags: list[AssemblyFragment] = []
        tool_frags: list[AssemblyFragment] = []
        ordinary_frags: list[AssemblyFragment] = []
        for fragment in fragments:
            if (fragment.always or fragment.source in _PROTECTED_SOURCES
                    or (fragment.source == 'activity' and fragment.kind != 'recall_excerpt')):
                protected_frags.append(fragment)
            elif fragment.source in {"person_experience", "person_portrait", "memory", "manual_history"} or fragment.kind == 'recall_excerpt':
                memory_frags.append(fragment)
            elif fragment.source == "tool":
                tool_frags.append(fragment)
            else:
                ordinary_frags.append(fragment)

        kept: list[AssemblyFragment] = list(protected_frags)
        budget_left = limit or 0
        selected_memory_count = 0
        for fragment in (*memory_frags, *ordinary_frags):
            if (fragment.source == 'memory' and ctx.recall_request is not None
                    and ctx.recall_request.selected_limit is not None
                    and selected_memory_count >= max(0, ctx.recall_request.selected_limit)):
                skipped_by_source.setdefault(fragment.source, []).append(fragment.id)
                reasons.setdefault(fragment.source, []).append((fragment.id, 'selected_limit'))
                continue
            if len(fragment.content) > budget_left and fragment.alternatives:
                fitting = [text for text in fragment.alternatives if text.strip() and len(text) <= budget_left]
                if fitting:
                    fragment = replace(fragment, content=max(fitting, key=len),
                                       alternatives=tuple(text for text in fragment.alternatives if len(text) <= budget_left))
            if budget_left <= 0 or max(len(fragment.content), 1) > budget_left:
                skipped_by_source.setdefault(fragment.source, []).append(
                    fragment.id
                )
                reasons.setdefault(fragment.source, []).append((fragment.id, 'budget'))
                continue
            if fragment.alternatives or fragment.portrait_levels:
                cap = len(fragment.content)
                fragment = replace(fragment,
                    alternatives=tuple(text for text in fragment.alternatives if len(text) <= cap),
                    portrait_levels=tuple((key, text) for key, text in fragment.portrait_levels if len(text) <= cap))
            kept.append(fragment)
            if fragment.source == 'memory':
                selected_memory_count += 1
            budget_left -= max(len(fragment.content), 1)
        kept_fragments = tuple(kept)
        tool_input = "\n".join(
            fragment.content.strip()
            for fragment in tool_frags
            if fragment.content.strip()
        )

        reports = tuple(
            SourceLoadReport(
                source=source.name,
                status=source.status,
                loaded_ids=tuple(
                    fragment.id
                    for fragment in (
                        tool_frags if source.name == "tool" else kept_fragments
                    )
                    if fragment.source == source.name
                ),
                skipped_ids=tuple(skipped_by_source.get(source.name, ())),
                budget=limit or 0,
                error=errors.get(source.name),
                elapsed_ms=timings.get(source.name, 0.0),
                skipped_reasons=tuple(reasons.get(source.name, ())),
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
            tool_input=tool_input,
            recall_batch=recall_batch,
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
            # 身份源缺失时不得硬造"匠石"身份；置空并由装载报告标记该源错误。
            identity_summary=first("identity", "identity_summary"),
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
            provenance=Provenance(
                source="assembled_current_state",
                method="load_existing_only",
            ),
        )
