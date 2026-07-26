from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from jshi.core import Provenance, SubjectState
from jshi.identity import IdentityRepository
from jshi.memory import InProcessHistoryMemory, MemoryPort, RecalledFragment
from jshi.models import ModelPort, ModelRequest

from .domain import (
    ALLOWED_EPISTEMIC_TRANSITIONS,
    Activity,
    ActivityKind,
    ActivityStatus,
    CognitiveContent,
    CognitiveKind,
    EpistemicStatus,
    EvidenceKind,
    HistoryKind,
    HistoryRecord,
    PersonalItem,
    PersonalKind,
    PersonalStatus,
    StateTransition,
)
from .repository import SubjectRepository


@dataclass(frozen=True)
class AssembledCurrentState:
    """Pre-model working set. Never invents subject-facing open matter."""

    input_text: str
    subject_state: SubjectState
    personal_items: tuple[PersonalItem, ...]
    open_matter_ids: tuple[str, ...]
    recalled: tuple[RecalledFragment, ...]


@dataclass(frozen=True)
class SubjectActivityResult:
    activity: Activity
    perception: CognitiveContent
    thought: CognitiveContent
    action_text: str
    current_state: AssembledCurrentState


class SubjectProcess:
    """Minimal subject loop: assemble current state, cognize, act, record."""

    def __init__(
        self,
        repository: SubjectRepository,
        identities: IdentityRepository,
        cognition: ModelPort,
        memory: MemoryPort | None = None,
    ) -> None:
        self.repository = repository
        self.identities = identities
        self.cognition = cognition
        self.memory = memory or InProcessHistoryMemory(repository)

    def assemble_current_state(
        self, subject_id: str, input_text: str
    ) -> AssembledCurrentState:
        """Build the pre-model current state from existing records only."""
        personal = tuple(self._relevant_personal_world(subject_id))
        open_matter = tuple(
            item for item in personal if item.kind is PersonalKind.CONCERN
        )
        recalled = tuple(self.memory.recall(subject_id, input_text))
        subject_state = self._subject_state(subject_id, personal)
        return AssembledCurrentState(
            input_text=input_text,
            subject_state=subject_state,
            personal_items=personal,
            open_matter_ids=tuple(item.id for item in open_matter),
            recalled=recalled,
        )

    def experience(self, subject_id: str, text: str) -> SubjectActivityResult:
        fact = HistoryRecord(
            subject_id=subject_id,
            kind=HistoryKind.FACT,
            event_type="external_input",
            content={"text": text, "source": "human"},
        )
        self.repository.add_history(fact)

        current = self.assemble_current_state(subject_id, text)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="current_state_assembled",
                content={
                    "input": text,
                    "open_matter_ids": list(current.open_matter_ids),
                    "value_count": len(current.subject_state.salient_values),
                    "commitment_count": len(current.subject_state.commitments),
                    "recalled_event_ids": [item.event_id for item in current.recalled],
                },
                source_ids=(fact.id, *current.open_matter_ids),
            )
        )

        activity = Activity(
            subject_id=subject_id,
            kind=ActivityKind.EXTERNAL,
            trigger=fact.id,
            active_concern_ids=current.open_matter_ids,
        )
        self.repository.add_activity(activity)

        perception = CognitiveContent(
            subject_id=subject_id,
            activity_id=activity.id,
            kind=CognitiveKind.PERCEPTION,
            content=f"对方表达：{text}",
            epistemic_status=EpistemicStatus.ACCEPTED,
            evidence_kind=EvidenceKind.REPORT,
            source_ids=(fact.id,),
        )
        self.repository.add_cognitive_content(perception)

        model_context = tuple(
            {
                "id": item.id,
                "kind": item.kind.value,
                "content": item.content,
                "status": item.status.value,
            }
            for item in current.personal_items
        ) + tuple(
            {
                "id": item.event_id,
                "kind": "recalled_fact",
                "content": item.text,
                "status": "active",
                "event_type": item.event_type,
            }
            for item in current.recalled
        )
        response = self.cognition.generate(
            ModelRequest(
                purpose="subject_activity",
                input_text=text,
                subject_state=current.subject_state,
                context=model_context,
            )
        )
        thought = CognitiveContent(
            subject_id=subject_id,
            activity_id=activity.id,
            kind=CognitiveKind.INFERENCE,
            content=response.text,
            epistemic_status=EpistemicStatus.CONSIDERING,
            evidence_kind=EvidenceKind.COGNITIVE_REASONING,
            source_ids=(
                perception.id,
                *(item.id for item in current.personal_items),
                *(item.event_id for item in current.recalled),
            ),
            model=response.model,
        )
        self.repository.add_cognitive_content(thought)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="cognitive_content_appeared",
                content={
                    "activity_id": activity.id,
                    "cognitive_content_id": thought.id,
                    "content": thought.content,
                    "epistemic_status": thought.epistemic_status.value,
                },
                source_ids=thought.source_ids,
            )
        )

        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.FACT,
                event_type="language_action",
                content={
                    "activity_id": activity.id,
                    "text": response.text,
                    "model": response.model,
                },
                source_ids=(thought.id,),
            )
        )
        completed = self.repository.update_activity(
            activity.id, status=ActivityStatus.COMPLETED
        )
        return SubjectActivityResult(
            completed, perception, thought, response.text, current
        )

    def reflect(self, subject_id: str, prompt: str) -> CognitiveContent:
        current = self.assemble_current_state(subject_id, prompt)
        recent_history = self.repository.list_history(subject_id, limit=20)
        activity = Activity(
            subject_id=subject_id,
            kind=ActivityKind.INTERNAL,
            trigger=prompt,
            active_concern_ids=current.open_matter_ids,
        )
        self.repository.add_activity(activity)
        history_text = "\n".join(
            f"{item.kind.value}:{item.event_type}:{dict(item.content)}"
            for item in recent_history
        )
        response = self.cognition.generate(
            ModelRequest(
                purpose="reflection",
                input_text=f"{prompt}\n近期历史：\n{history_text}",
                subject_state=current.subject_state,
                context=tuple(
                    {"kind": item.kind.value, "content": item.content}
                    for item in current.personal_items
                ),
            )
        )
        reflection = CognitiveContent(
            subject_id=subject_id,
            activity_id=activity.id,
            kind=CognitiveKind.EVALUATION,
            content=response.text,
            epistemic_status=EpistemicStatus.CONSIDERING,
            evidence_kind=EvidenceKind.COGNITIVE_REASONING,
            source_ids=tuple(item.id for item in recent_history),
            model=response.model,
        )
        self.repository.add_cognitive_content(reflection)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="reflection",
                content={
                    "activity_id": activity.id,
                    "cognitive_content_id": reflection.id,
                    "content": reflection.content,
                    "epistemic_status": reflection.epistemic_status.value,
                },
                source_ids=reflection.source_ids,
            )
        )
        self.repository.update_activity(activity.id, status=ActivityStatus.COMPLETED)
        return reflection

    def transition_cognition(
        self,
        content_id: str,
        to_status: EpistemicStatus,
        reason: str,
        source_ids: tuple[str, ...] = (),
    ) -> CognitiveContent:
        current = self.repository.get_cognitive_content(content_id)
        allowed = ALLOWED_EPISTEMIC_TRANSITIONS[current.epistemic_status]
        if to_status not in allowed:
            raise ValueError(
                f"Invalid epistemic transition: "
                f"{current.epistemic_status.value} -> {to_status.value}"
            )
        transition = StateTransition(
            subject_id=current.subject_id,
            target_type="cognitive_content",
            target_id=current.id,
            from_state=current.epistemic_status.value,
            to_state=to_status.value,
            reason=reason,
            source_ids=source_ids,
        )
        updated = self.repository.update_epistemic_status(content_id, to_status)
        self.repository.add_transition(transition)
        self.repository.add_history(
            HistoryRecord(
                subject_id=current.subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="epistemic_transition",
                content={
                    "cognitive_content_id": content_id,
                    "from": transition.from_state,
                    "to": transition.to_state,
                    "reason": reason,
                },
                source_ids=(transition.id, *source_ids),
            )
        )
        return updated

    def add_personal_item(
        self,
        subject_id: str,
        kind: PersonalKind,
        content: str,
        source_ids: tuple[str, ...] = (),
    ) -> PersonalItem:
        item = PersonalItem(
            subject_id=subject_id,
            kind=kind,
            content=content,
            source_ids=source_ids,
        )
        self.repository.add_personal_item(item)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="personal_item_created",
                content={
                    "personal_item_id": item.id,
                    "kind": item.kind.value,
                    "content": item.content,
                },
                source_ids=source_ids,
            )
        )
        return item

    def propose_open_matter(
        self,
        subject_id: str,
        content: str,
        *,
        source_ids: tuple[str, ...],
    ) -> PersonalItem:
        """Record subject-facing open matter after cognition, with required sources.

        This is never called by assemble_current_state. Call it only once a
        cognitive result (or human judgment) has identified unfinished follow-through.
        """
        if not source_ids:
            raise ValueError(
                "Subject-facing open matter requires source_ids "
                "(cognitive content or fact ids)"
            )
        return self.add_personal_item(
            subject_id, PersonalKind.CONCERN, content, source_ids=source_ids
        )

    def close_personal_item(
        self, item_id: str, status: PersonalStatus, reason: str
    ) -> PersonalItem:
        if status is PersonalStatus.ACTIVE:
            raise ValueError("Closing a personal item requires a non-active status")
        current = self.repository.get_personal_item(item_id)
        updated = self.repository.update_personal_status(item_id, status)
        transition = StateTransition(
            subject_id=current.subject_id,
            target_type="personal_item",
            target_id=item_id,
            from_state=current.status.value,
            to_state=status.value,
            reason=reason,
        )
        self.repository.add_transition(transition)
        self.repository.add_history(
            HistoryRecord(
                subject_id=current.subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="personal_item_closed",
                content={
                    "personal_item_id": item_id,
                    "kind": current.kind.value,
                    "to": status.value,
                    "reason": reason,
                },
                source_ids=(transition.id,),
            )
        )
        return updated

    def _relevant_personal_world(
        self, subject_id: str, limit: int = 50
    ) -> Sequence[PersonalItem]:
        # Active items only; transparent selection for the minimal experiment.
        items = self.repository.list_personal_items(subject_id, active_only=True)
        return items[-limit:]

    def _subject_state(
        self, subject_id: str, personal: Sequence[PersonalItem]
    ) -> SubjectState:
        identity = self.identities.get(subject_id)
        values = tuple(
            item.content for item in personal if item.kind is PersonalKind.VALUE
        )
        concerns = tuple(
            item.content for item in personal if item.kind is PersonalKind.CONCERN
        )
        commitments = tuple(
            item.content for item in personal if item.kind is PersonalKind.COMMITMENT
        )
        return SubjectState(
            subject_id=subject_id,
            identity_summary=f"{identity.name}；来源：{identity.origin}",
            current_stance=identity.narrative,
            salient_values=values,
            commitments=commitments,
            concerns=concerns,
            provenance=Provenance(
                source="assembled_current_state",
                method="load_existing_only",
            ),
        )
