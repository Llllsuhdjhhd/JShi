from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Mapping, Sequence

from .port import (
    ContextAssessment,
    ContextViewState,
    ExperienceLedgerPort,
    ExperienceSegment,
    ActorKind,
    ConsumerKind,
    MemoryBatch,
    MemoryIngestLedgerEntry,
    OutputKind,
    SegmentStatus,
    empty_context_view,
    new_id,
    utc_now,
)

_SENTENCE_RE = re.compile(r".+?(?:[。！？.!?]+|$)", re.S)


def split_sentences(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(part for part in _SENTENCE_RE.findall(text) if part.strip())


@dataclass(frozen=True)
class _Sentence:
    ref: str
    segment_id: str
    text: str


@dataclass
class _SubjectLedgerState:
    segments: list[ExperienceSegment] = field(default_factory=list)
    cursors: dict[ConsumerKind, int] = field(default_factory=dict)
    ingest_entries: list[MemoryIngestLedgerEntry] = field(default_factory=list)
    context: ContextViewState = field(default_factory=empty_context_view)
    pending_sequences: list[int] = field(default_factory=list)
    sentences: list[_Sentence] = field(default_factory=list)


class InProcessExperienceLedger(ExperienceLedgerPort):
    """最小活动日志：外部输入、主体回复、内部活动都按时间顺序追加。"""

    def __init__(
        self,
        *,
        active_window_chars: int = 2000,
        memory_batch_chars: int = 2000,
        memory_batch_segments: int = 3,
        memory_batch_max_age_seconds: float = 3600,
    ) -> None:
        self.active_window_chars = active_window_chars
        self.memory_batch_chars = memory_batch_chars
        self.memory_batch_segments = memory_batch_segments
        self.memory_batch_max_age_seconds = memory_batch_max_age_seconds
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

    def append_external(
        self,
        subject_id: str,
        *,
        actor_object_id: str,
        text_raw: str,
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
            state_delta=state_delta,
            response_plan=dict(response_plan) if response_plan else None,
            response_statuses=tuple(dict.fromkeys(response_statuses)),
            source_ids=tuple(dict.fromkeys(source_ids)),
            occurred_at=occurred_at or utc_now(),
        )
        state.segments.append(segment)
        state.pending_sequences.append(sequence)
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
        excluded = set(state.context.excluded_sentence_refs)
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

        if allow_edit:
            visible = self._visible_sentences(state, excluded)
            trim_targets = assessment.trim_refs if (
                assessment.need_trim or assessment.trim_refs
            ) else ()
            if trim_targets:
                sentence_trims = []
                for raw in trim_targets:
                    if self._is_protected_ref(raw, protected, speaker_object_id or state.context.speaker_object_id):
                        continue
                    if raw in excerpts or raw.replace("memory:", "") in {
                        key.replace("memory:", "") for key in excerpts
                    }:
                        for key in list(excerpts):
                            if key == raw or key.endswith(raw) or raw.endswith(key.replace("memory:", "")):
                                del excerpts[key]
                                changed = True
                        continue
                    sentence_trims.append(raw)
                if sentence_trims:
                    excluded.update(self._resolve_refs(sentence_trims, visible))
                    changed = True
            if assessment.need_focus or assessment.focus_refs:
                focused = list(
                    dict.fromkeys(
                        (
                            *focused,
                            *self._resolve_refs(
                                assessment.focus_refs,
                                self._visible_sentences(state, excluded),
                            ),
                        )
                    )
                )
                changed = True

        last_applied = state.context.last_applied_sequence
        segment_refs = list(state.context.segment_refs)
        if state.pending_sequences:
            by_sequence = {segment.sequence: segment for segment in state.segments}
            for sequence in state.pending_sequences:
                segment = by_sequence.get(sequence)
                if segment is None:
                    continue
                if segment.segment_id not in segment_refs:
                    segment_refs.append(segment.segment_id)
                for index, text in enumerate(split_sentences(segment.text_raw or "")):
                    state.sentences.append(
                        _Sentence(
                            ref=f"{segment.segment_id}:{index}",
                            segment_id=segment.segment_id,
                            text=text,
                        )
                    )
                last_applied = max(last_applied, sequence)
            state.pending_sequences.clear()
            changed = True

        kept_speaker = speaker_object_id or state.context.speaker_object_id
        if speaker_object_id and speaker_object_id != state.context.speaker_object_id:
            changed = True

        if not changed:
            return state.context

        state.context = ContextViewState(
            version=state.context.version + 1,
            context_text=self._render_context(state, excluded, excerpts),
            segment_refs=tuple(segment_refs),
            recall_excerpts=tuple(excerpts.items()),
            speaker_object_id=kept_speaker,
            excluded_sentence_refs=tuple(dict.fromkeys(excluded)),
            focused_refs=tuple(focused),
            last_applied_sequence=last_applied,
        )
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
        return ContextAssessment(
            need_trim=bool(getattr(assessment, "need_trim", False)),
            need_focus=bool(getattr(assessment, "need_focus", False)),
            trim_refs=tuple(getattr(assessment, "trim_refs", ()) or ()),
            focus_refs=tuple(getattr(assessment, "focus_refs", ()) or ()),
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
    def _visible_sentences(
        state: _SubjectLedgerState,
        excluded: set[str],
    ) -> list[_Sentence]:
        return [sentence for sentence in state.sentences if sentence.ref not in excluded]

    @staticmethod
    def _resolve_refs(
        refs: Sequence[str],
        visible: Sequence[_Sentence],
    ) -> tuple[str, ...]:
        resolved: list[str] = []
        visible_refs = {sentence.ref for sentence in visible}
        for raw in refs:
            if raw in visible_refs:
                resolved.append(raw)
                continue
            if raw.isdigit():
                index = int(raw) - 1
                if 0 <= index < len(visible):
                    resolved.append(visible[index].ref)
                continue
            for sentence in visible:
                if sentence.segment_id == raw:
                    resolved.append(sentence.ref)
        return tuple(dict.fromkeys(resolved))

    @staticmethod
    def _render_context(
        state: _SubjectLedgerState,
        excluded: set[str],
        excerpts: Mapping[str, str] | None = None,
    ) -> str:
        chunks: list[str] = []
        previous_segment = None
        for sentence in state.sentences:
            if sentence.ref in excluded:
                continue
            if previous_segment is not None and sentence.segment_id != previous_segment:
                chunks.append("\n")
            chunks.append(sentence.text)
            previous_segment = sentence.segment_id
        body = "".join(chunks)
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

    def build_memory_batch(
        self,
        subject_id: str,
        *,
        min_chars: int | None = None,
        min_segments: int | None = None,
        max_age_seconds: float | None = None,
    ) -> MemoryBatch | None:
        state = self._state(subject_id)
        memory_start = state.cursors.get(ConsumerKind.MEMORY, 0)
        pending = [
            segment for segment in state.segments if segment.sequence > memory_start
        ]
        if not pending:
            return None

        chars = sum(len(segment.text_raw or "") for segment in pending)
        required_chars = min_chars or self.memory_batch_chars
        required_segments = min_segments or self.memory_batch_segments
        max_age = (
            max_age_seconds
            if max_age_seconds is not None
            else self.memory_batch_max_age_seconds
        )

        oldest = pending[0]
        age_seconds = (utc_now() - oldest.occurred_at).total_seconds()
        triggered = (
            chars >= required_chars
            or len(pending) >= required_segments
            or age_seconds >= max_age
        )
        if not triggered:
            return None

        first_external = next(
            (
                segment.actor_object_id
                for segment in pending
                if segment.actor_kind == ActorKind.EXTERNAL
                and segment.actor_object_id
            ),
            None,
        )
        object_ids = tuple(
            dict.fromkeys(
                object_id
                for segment in pending
                for object_id in (
                    (
                        segment.actor_object_id
                        if segment.actor_kind == ActorKind.EXTERNAL
                        else None
                    ),
                    *segment.mentioned_object_ids,
                )
                if object_id
            )
        )
        object_sources: dict[str, tuple[str, ...]] = {}
        for segment in pending:
            candidate_ids = []
            if segment.actor_kind == ActorKind.EXTERNAL and segment.actor_object_id:
                candidate_ids.append(segment.actor_object_id)
            candidate_ids.extend(segment.mentioned_object_ids)
            for object_id in dict.fromkeys(candidate_ids):
                object_sources.setdefault(object_id, [])
                object_sources[object_id].append(segment.segment_id)
        source_ids = tuple(
            dict.fromkeys(
                source_id
                for segment in pending
                for source_id in (segment.segment_id, *segment.source_ids)
            )
        )
        return MemoryBatch(
            batch_id=new_id(),
            subject_id=subject_id,
            external_object_id=first_external,
            object_ids=object_ids,
            object_sources={
                object_id: tuple(dict.fromkeys(segment_ids))
                for object_id, segment_ids in object_sources.items()
            },
            from_sequence=oldest.sequence,
            to_sequence=pending[-1].sequence,
            segments=tuple(pending),
            source_ids=source_ids,
        )

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
        return updated

    def consumer_cursor(
        self,
        subject_id: str,
        consumer: ConsumerKind,
    ) -> int:
        return self._state(subject_id).cursors.get(consumer, 0)

    def head_sequence(self, subject_id: str) -> int:
        state = self._states.get(subject_id)
        if not state or not state.segments:
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
        return archived

    def register_ingest(self, batch: MemoryBatch) -> MemoryIngestLedgerEntry:
        state = self._state(batch.subject_id)
        entry = MemoryIngestLedgerEntry(
            ingest_id=new_id(),
            batch_id=batch.batch_id,
        )
        state.ingest_entries.append(entry)
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
        for state in self._states.values():
            for index, entry in enumerate(state.ingest_entries):
                if entry.ingest_id == updated.ingest_id:
                    state.ingest_entries[index] = updated
                    return
        raise KeyError(updated.ingest_id)
