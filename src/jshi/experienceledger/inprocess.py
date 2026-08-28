from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Mapping, Sequence

from jshi.core.params import ACTIVE_ZONE_CHARS

from .port import (
    ContextAssessment,
    ContextViewState,
    ExperienceLedgerPort,
    ExperienceSegment,
    ActorKind,
    ConsumerKind,
    MemoryIngestLedgerEntry,
    OutputKind,
    SegmentStatus,
    empty_context_view,
    new_id,
    utc_now,
)

@dataclass
class _SubjectLedgerState:
    segments: list[ExperienceSegment] = field(default_factory=list)
    cursors: dict[ConsumerKind, int] = field(default_factory=dict)
    ingest_entries: list[MemoryIngestLedgerEntry] = field(default_factory=list)
    context: ContextViewState = field(default_factory=empty_context_view)
    pending_sequences: list[int] = field(default_factory=list)


class InProcessExperienceLedger(ExperienceLedgerPort):
    """最小活动日志：外部输入、主体回复、内部活动都按时间顺序追加。"""

    def __init__(
        self,
        *,
        active_window_chars: int = ACTIVE_ZONE_CHARS,
    ) -> None:
        self.active_window_chars = active_window_chars
        self._states: dict[str, _SubjectLedgerState] = {}

    def _state(self, subject_id: str) -> _SubjectLedgerState:
        state = self._states.get(subject_id)
        if state is None:
            state = _SubjectLedgerState(
                cursors={
                    ConsumerKind.ACTIVE_ZONE: 0,
                    ConsumerKind.MEMORY: 0,
                    ConsumerKind.AUDIT: 0,
                }
            )
            self._states[subject_id] = state
        return state

    def _after_write(self, subject_id: str) -> None:
        """落盘钩子。内存实现为空。"""

    def append_external(
        self,
        subject_id: str,
        *,
        actor_object_id: str,
        text_raw: str,
        objects: Mapping[str, str] | None = None,
        mentioned_object_ids: Sequence[str] = (),
        source_ids: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment:
        return self._append(
            subject_id=subject_id,
            actor_kind=ActorKind.EXTERNAL,
            output_kind=OutputKind.EXTERNAL_INPUT,
            actor_object_id=actor_object_id,
            text_raw=text_raw,
            objects=objects,
            state_delta=None,
            response_statuses=(),
            mentioned_object_ids=mentioned_object_ids,
            source_ids=source_ids,
            occurred_at=occurred_at,
        )

    def append_subject_reply(
        self,
        subject_id: str,
        *,
        text_raw: str,
        objects: Mapping[str, str] | None = None,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        state_delta: Mapping[str, object] | None = None,
        response_plan: Mapping[str, object] | None = None,
        response_statuses: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment:
        return self._append(
            subject_id=subject_id,
            actor_kind=ActorKind.SUBJECT,
            output_kind=OutputKind.SUBJECT_REPLY,
            actor_object_id=None,
            text_raw=text_raw,
            objects=objects,
            state_delta=dict(state_delta) if state_delta else None,
            response_plan=dict(response_plan) if response_plan else None,
            response_statuses=tuple(dict.fromkeys(response_statuses)),
            mentioned_object_ids=mentioned_object_ids,
            source_ids=source_ids,
            occurred_at=occurred_at,
        )

    def append_subject_state(
        self,
        subject_id: str,
        *,
        state_delta: Mapping[str, object],
        objects: Mapping[str, str] | None = None,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        response_plan: Mapping[str, object] | None = None,
        response_statuses: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment:
        return self._append(
            subject_id=subject_id,
            actor_kind=ActorKind.SUBJECT,
            output_kind=OutputKind.SUBJECT_STATE,
            actor_object_id=None,
            text_raw=None,
            objects=objects,
            state_delta=dict(state_delta),
            response_plan=dict(response_plan) if response_plan else None,
            response_statuses=tuple(dict.fromkeys(response_statuses)),
            mentioned_object_ids=mentioned_object_ids,
            source_ids=source_ids,
            occurred_at=occurred_at,
        )

    def append_subject_silent(
        self,
        subject_id: str,
        *,
        objects: Mapping[str, str] | None = None,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        response_plan: Mapping[str, object] | None = None,
        response_statuses: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment:
        return self._append(
            subject_id=subject_id,
            actor_kind=ActorKind.SUBJECT,
            output_kind=OutputKind.SUBJECT_SILENT,
            actor_object_id=None,
            text_raw=None,
            objects=objects,
            state_delta=None,
            response_plan=dict(response_plan) if response_plan else None,
            response_statuses=tuple(dict.fromkeys(response_statuses)),
            mentioned_object_ids=mentioned_object_ids,
            source_ids=source_ids,
            occurred_at=occurred_at,
        )

    def append_internal(
        self,
        subject_id: str,
        *,
        text_raw: str | None = None,
        objects: Mapping[str, str] | None = None,
        state_delta: Mapping[str, object] | None = None,
        source_ids: Sequence[str] = (),
        mentioned_object_ids: Sequence[str] = (),
        occurred_at: datetime | None = None,
    ) -> ExperienceSegment:
        return self._append(
            subject_id=subject_id,
            actor_kind=ActorKind.SYSTEM,
            output_kind=OutputKind.INTERNAL,
            actor_object_id=None,
            text_raw=text_raw,
            objects=objects,
            state_delta=dict(state_delta) if state_delta else None,
            response_statuses=(),
            mentioned_object_ids=mentioned_object_ids,
            source_ids=source_ids,
            occurred_at=occurred_at,
        )

    def _append(
        self,
        *,
        subject_id: str,
        actor_kind: ActorKind,
        output_kind: OutputKind,
        actor_object_id: str | None,
        text_raw: str | None,
        objects: Mapping[str, str] | None = None,
        state_delta: Mapping[str, object] | None,
        response_statuses: Sequence[str],
        mentioned_object_ids: Sequence[str],
        source_ids: Sequence[str],
        occurred_at: datetime | None,
        response_plan: Mapping[str, object] | None = None,
    ) -> ExperienceSegment:
        state = self._state(subject_id)
        sequence = self.head_sequence(subject_id) + 1
        segment = ExperienceSegment(
            segment_id=new_id(),
            sequence=sequence,
            subject_id=subject_id,
            actor_kind=actor_kind,
            output_kind=output_kind,
            actor_object_id=actor_object_id,
            mentioned_object_ids=tuple(dict.fromkeys(mentioned_object_ids)),
            text_raw=text_raw,
            objects=dict(objects) if objects else None,
            state_delta=state_delta,
            response_plan=dict(response_plan) if response_plan else None,
            response_statuses=tuple(dict.fromkeys(response_statuses)),
            source_ids=tuple(dict.fromkeys(source_ids)),
            occurred_at=occurred_at or utc_now(),
        )
        state.segments.append(segment)
        state.pending_sequences.append(sequence)
        self._after_write(subject_id)
        return segment

    def current_context_view(self, subject_id: str) -> ContextViewState:
        return self._state(subject_id).context

    def apply_context_assessment(
        self,
        subject_id: str,
        assessment: ContextAssessment | None = None,
        *,
        allow_edit: bool = True,
        recall_excerpts: Sequence[tuple[str, str]] = (),
        speaker_object_id: str | None = None,
        protected_refs: Sequence[str] = (),
    ) -> ContextViewState:
        state = self._state(subject_id)
        assessment = self._coerce_assessment(assessment)
        changed = False
        focused = list(state.context.focused_refs)
        protected = set(protected_refs)
        excerpts = {
            ref: text for ref, text in state.context.recall_excerpts
        }
        for ref, text in recall_excerpts:
            if not ref:
                continue
            if excerpts.get(ref) != text:
                excerpts[ref] = text
                changed = True

        segment_refs = list(state.context.segment_refs)
        segments_by_id = {segment.segment_id: segment for segment in state.segments}

        last_applied = state.context.last_applied_sequence
        if state.pending_sequences:
            by_sequence = {segment.sequence: segment for segment in state.segments}
            for sequence in state.pending_sequences:
                segment = by_sequence.get(sequence)
                if segment is None:
                    continue
                if segment.segment_id not in segment_refs:
                    segment_refs.append(segment.segment_id)
                last_applied = max(last_applied, sequence)
            state.pending_sequences.clear()
            changed = True

        if allow_edit:
            # 删除段 / 回忆摘录（作用于合并后的完整视图；remove 与 drop_recall 合并处理）
            remove_refs = tuple(
                dict.fromkeys((*assessment.remove, *assessment.drop_recall))
            )
            for raw in remove_refs:
                if self._is_protected_ref(
                    raw, protected, speaker_object_id or state.context.speaker_object_id
                ):
                    continue
                if self._drop_excerpt(raw, excerpts):
                    changed = True
                    continue
                if raw in segments_by_id or raw in segment_refs:
                    segment_refs = [ref for ref in segment_refs if ref != raw]
                    focused = [ref for ref in focused if ref != raw]
                    changed = True
            # 聚焦（段级）
            for raw in assessment.focus:
                if raw in segments_by_id and raw not in focused:
                    focused.append(raw)
                    changed = True

        kept_speaker = speaker_object_id or state.context.speaker_object_id
        if speaker_object_id and speaker_object_id != state.context.speaker_object_id:
            changed = True

        if allow_edit:
            capped, capped_changed = self._enforce_cap(
                state, segment_refs, kept_speaker, protected
            )
            segment_refs = capped
            changed = changed or capped_changed

        if not changed:
            return state.context

        state.context = ContextViewState(
            version=state.context.version + 1,
            context_text=self._render_context(state, segment_refs, excerpts),
            segment_refs=tuple(segment_refs),
            segment_texts=tuple(
                (ref, segment.text_raw)
                for ref in segment_refs
                if (segment := segments_by_id.get(ref)) is not None
                and segment.text_raw
            ),
            recall_excerpts=tuple(excerpts.items()),
            speaker_object_id=kept_speaker,
            focused_refs=tuple(focused),
            last_applied_sequence=last_applied,
        )
        self._after_write(subject_id)
        return state.context

    def list_experiences(
        self,
        subject_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[ExperienceSegment, ...]:
        return tuple(
            segment
            for segment in self._state(subject_id).segments
            if segment.sequence > after_sequence
        )

    @staticmethod
    def _coerce_assessment(
        assessment: ContextAssessment | object | None,
    ) -> ContextAssessment:
        if assessment is None:
            return ContextAssessment()
        if isinstance(assessment, ContextAssessment):
            return assessment
        remove = tuple(getattr(assessment, "remove", ()) or ())
        if not remove:
            remove = tuple(getattr(assessment, "trim_refs", ()) or ())
        drop_recall = tuple(getattr(assessment, "drop_recall", ()) or ())
        focus = tuple(getattr(assessment, "focus", ()) or ())
        if not focus:
            focus = tuple(getattr(assessment, "focus_refs", ()) or ())
        return ContextAssessment(
            remove=remove,
            drop_recall=drop_recall,
            focus=focus,
        )

    @staticmethod
    def _is_protected_ref(
        raw: str,
        protected: set[str],
        speaker_object_id: str | None,
    ) -> bool:
        if raw in protected:
            return True
        if raw.startswith("object:") or raw.startswith("identity:") or raw.startswith("stance:"):
            return True
        if speaker_object_id and (
            raw == speaker_object_id or raw == f"object:{speaker_object_id}"
        ):
            return True
        return False

    @staticmethod
    def _drop_excerpt(raw: str, excerpts: dict[str, str]) -> bool:
        """按引用删除回忆摘录；命中返回 True。"""
        if raw in excerpts:
            del excerpts[raw]
            return True
        normalized = raw[7:] if raw.startswith("memory:") else raw
        if normalized:
            hits = [key for key in excerpts if key[7:] == normalized]
            for key in hits:
                del excerpts[key]
            return bool(hits)
        return False

    @staticmethod
    def _render_context(
        state: _SubjectLedgerState,
        segment_refs: Sequence[str],
        excerpts: Mapping[str, str] | None = None,
    ) -> str:
        by_id = {segment.segment_id: segment for segment in state.segments}
        parts: list[str] = []
        for ref in segment_refs:
            segment = by_id.get(ref)
            if segment is None or not segment.text_raw:
                continue
            parts.append(segment.text_raw)
        body = "\n".join(parts)
        if not excerpts:
            return body
        recall_block = "\n".join(
            f"[回忆 {ref}] {text}" for ref, text in excerpts.items() if text
        )
        if not recall_block:
            return body
        if body:
            return f"{body}\n{recall_block}"
        return recall_block

    def _enforce_cap(
        self,
        state: _SubjectLedgerState,
        segment_refs: Sequence[str],
        speaker_object_id: str | None,
        protected: set[str],
    ) -> tuple[list[str], bool]:
        """物理硬上限：超过 active_window_chars 时剔除最旧的非保护段。"""
        by_id = {segment.segment_id: segment for segment in state.segments}

        def is_protected(segment_id: str) -> bool:
            segment = by_id.get(segment_id)
            if segment is None:
                return True
            if segment_id in protected:
                return True
            if speaker_object_id and segment.actor_object_id == speaker_object_id:
                return True
            return False

        kept: list[str] = []
        total = 0
        dropped = 0
        for ref in reversed(segment_refs):
            segment = by_id.get(ref)
            size = len(segment.text_raw or "") if segment else 0
            if (total + size <= self.active_window_chars) or is_protected(ref):
                kept.append(ref)
                total += size
            else:
                dropped += 1
        kept.reverse()
        # 至少保留最新一段，避免视图为空
        if not kept and segment_refs:
            kept = [segment_refs[-1]]
            dropped -= 1
        return kept, dropped > 0

    def active_window(
        self,
        subject_id: str,
        *,
        limit_chars: int | None = None,
    ) -> tuple[ExperienceSegment, ...]:
        state = self._state(subject_id)
        start = state.cursors.get(ConsumerKind.ACTIVE_ZONE, 0)
        selected: list[ExperienceSegment] = []
        used = 0
        cap = limit_chars if limit_chars is not None else self.active_window_chars
        for segment in state.segments:
            if segment.sequence <= start:
                continue
            size = len(segment.text_raw or "")
            if used + size > cap and selected:
                break
            selected.append(segment)
            used += size
        return tuple(selected)

    def advance_consumer_cursor(
        self,
        subject_id: str,
        consumer: ConsumerKind,
        through_sequence: int,
    ) -> int:
        state = self._state(subject_id)
        head = self.head_sequence(subject_id)
        if through_sequence > head:
            raise ValueError(
                f"cursor {through_sequence} exceeds head sequence {head}"
            )
        current = state.cursors.get(consumer, 0)
        updated = max(current, through_sequence)
        state.cursors[consumer] = updated
        self._after_write(subject_id)
        return updated

    def consumer_cursor(
        self,
        subject_id: str,
        consumer: ConsumerKind,
    ) -> int:
        return self._state(subject_id).cursors.get(consumer, 0)

    def head_sequence(self, subject_id: str) -> int:
        state = self._state(subject_id)
        if not state.segments:
            return 0
        return state.segments[-1].sequence

    def safe_trim_point(self, subject_id: str) -> int:
        state = self._state(subject_id)
        if not state.segments:
            return 0
        return min(
            state.cursors.get(ConsumerKind.ACTIVE_ZONE, 0),
            state.cursors.get(ConsumerKind.MEMORY, 0),
            state.cursors.get(ConsumerKind.AUDIT, 0),
        )

    def archive_before(self, subject_id: str, sequence: int) -> int:
        state = self._state(subject_id)
        archived = 0
        for index, segment in enumerate(state.segments):
            if segment.sequence >= sequence:
                break
            if segment.status != SegmentStatus.ARCHIVED:
                state.segments[index] = replace(
                    segment,
                    status=SegmentStatus.ARCHIVED,
                )
                archived += 1
        if archived:
            self._after_write(subject_id)
        return archived

    def register_ingest(self, batch: MemoryBatch) -> MemoryIngestLedgerEntry:
        state = self._state(batch.subject_id)
        entry = MemoryIngestLedgerEntry(
            ingest_id=new_id(),
            batch_id=batch.batch_id,
        )
        state.ingest_entries.append(entry)
        self._after_write(batch.subject_id)
        return entry

    def mark_ingesting(self, ingest_id: str) -> MemoryIngestLedgerEntry:
        entry = self._ledger_entry(ingest_id)
        updated = replace(entry, status="ingesting", attempts=entry.attempts + 1)
        self._replace_ledger(updated)
        return updated

    def mark_ingested(
        self,
        ingest_id: str,
        *,
        memory_event_ids: Sequence[str] = (),
        stored_marks: Mapping[str, str] | None = None,
    ) -> MemoryIngestLedgerEntry:
        entry = self._ledger_entry(ingest_id)
        updated = replace(
            entry,
            status="ingested",
            attempts=entry.attempts + 1,
            memory_event_ids=tuple(dict.fromkeys(memory_event_ids)),
            stored_marks=dict(stored_marks or {}),
            ingested_at=utc_now(),
            reason="",
        )
        self._replace_ledger(updated)
        return updated

    def mark_failed(
        self,
        ingest_id: str,
        *,
        reason: str,
    ) -> MemoryIngestLedgerEntry:
        entry = self._ledger_entry(ingest_id)
        updated = replace(
            entry,
            status="failed",
            attempts=entry.attempts + 1,
            reason=reason,
        )
        self._replace_ledger(updated)
        return updated

    def list_ingest_entries(
        self,
        subject_id: str,
    ) -> tuple[MemoryIngestLedgerEntry, ...]:
        return tuple(self._state(subject_id).ingest_entries)

    def _ledger_entry(self, ingest_id: str) -> MemoryIngestLedgerEntry:
        for state in self._states.values():
            for entry in state.ingest_entries:
                if entry.ingest_id == ingest_id:
                    return entry
        raise KeyError(ingest_id)

    def _replace_ledger(self, updated: MemoryIngestLedgerEntry) -> None:
        for subject_id, state in self._states.items():
            for index, entry in enumerate(state.ingest_entries):
                if entry.ingest_id == updated.ingest_id:
                    state.ingest_entries[index] = updated
                    self._after_write(subject_id)
                    return
        raise KeyError(updated.ingest_id)
