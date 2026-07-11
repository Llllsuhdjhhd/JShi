from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from jshi.chance import ChancePolicy, NoChancePolicy
from jshi.core import (
    CandidateChange,
    Event,
    EventKind,
    PluginContext,
    Provenance,
    SubjectState,
    TruthStatus,
)
from jshi.governance import GovernanceService
from jshi.identity import IdentityRepository, SelfPort
from jshi.infrastructure import EventStore, PluginRegistry
from jshi.inner import InnerActivity
from jshi.models import ModelPort, ModelRequest


@dataclass(frozen=True)
class InteractionResult:
    input_event: Event
    output_event: Event
    subject_state: SubjectState
    accepted_changes: tuple[CandidateChange, ...] = ()
    rejected_changes: tuple[CandidateChange, ...] = ()


class Orchestrator:
    def __init__(
        self,
        event_store: EventStore,
        identities: IdentityRepository,
        self_port: SelfPort,
        plugins: PluginRegistry,
        model: ModelPort,
        governance: GovernanceService,
        chance: ChancePolicy | None = None,
    ) -> None:
        self.event_store = event_store
        self.identities = identities
        self.self_port = self_port
        self.plugins = plugins
        self.model = model
        self.governance = governance
        self.chance = chance or NoChancePolicy()

    def interact(self, subject_id: str, text: str) -> InteractionResult:
        input_event = Event(
            subject_id=subject_id,
            kind=EventKind.EXTERNAL_INPUT,
            content={"text": text},
            truth_status=TruthStatus.REPORTED,
            provenance=Provenance(source="human"),
        )
        return self._run(input_event, purpose="external", prompt=text)

    def run_inner(
        self, subject_id: str, activity: InnerActivity
    ) -> InteractionResult:
        truth_status = (
            TruthStatus.IMAGINED
            if activity.allow_imagination
            else TruthStatus.REFLECTION
        )
        input_event = Event(
            subject_id=subject_id,
            kind=EventKind.INNER_ACTIVITY,
            content={
                "kind": activity.kind.value,
                "prompt": activity.prompt,
                "allow_imagination": activity.allow_imagination,
            },
            truth_status=truth_status,
            provenance=Provenance(
                source="inner_activity",
                source_event_ids=activity.source_event_ids,
            ),
        )
        return self._run(input_event, purpose="inner", prompt=activity.prompt)

    def _run(self, input_event: Event, purpose: str, prompt: str) -> InteractionResult:
        self.event_store.append(input_event)
        recent = self.event_store.list_for_subject(input_event.subject_id, limit=20)
        context = PluginContext(input_event.subject_id, input_event, recent)
        available_contributions = []
        for plugin in self.plugins.all():
            try:
                available_contributions.append(plugin.contribute(context))
            except Exception as error:
                self._record_plugin_failure(
                    input_event.subject_id, input_event, plugin.manifest.name, error
                )
        contributions = self.chance.select_attention(
            available_contributions, len(available_contributions)
        )
        identity = self.identities.get(input_event.subject_id)
        subject_state = self.self_port.synthesize(identity, input_event, contributions)
        response = self.model.generate(
            ModelRequest(
                purpose=purpose,
                input_text=prompt,
                subject_state=subject_state,
                context=contributions,
            )
        )

        output_truth = (
            input_event.truth_status
            if purpose == "inner"
            else TruthStatus.INFERRED
        )
        output_event = Event(
            subject_id=input_event.subject_id,
            kind=EventKind.ACTION if purpose == "external" else EventKind.DERIVED,
            content={"text": response.text, "purpose": purpose},
            truth_status=output_truth,
            provenance=Provenance(
                source="model",
                source_event_ids=(input_event.id,),
                method="generate",
                model=response.model,
            ),
        )
        self.event_store.append(output_event)

        proposed: list[CandidateChange] = []
        for plugin in self.plugins.all():
            try:
                proposed.extend(plugin.propose_changes(context, output_event))
            except Exception as error:
                self._record_plugin_failure(
                    input_event.subject_id, output_event, plugin.manifest.name, error
                )
        decision = self.governance.review(proposed)
        self._record_changes(decision.accepted, output_event)
        for change in decision.accepted:
            self.identities.accept(change)

        return InteractionResult(
            input_event=input_event,
            output_event=output_event,
            subject_state=subject_state,
            accepted_changes=decision.accepted,
            rejected_changes=decision.rejected,
        )

    def _record_changes(
        self, changes: Sequence[CandidateChange], outcome: Event
    ) -> None:
        for change in changes:
            event = Event(
                subject_id=change.subject_id,
                kind=EventKind.DERIVED,
                content={
                    "candidate_change": {
                        "id": change.id,
                        "target": change.target,
                        "operation": change.operation,
                        "value": dict(change.value),
                        "confidence": change.confidence,
                        "significance": change.significance,
                    },
                    "outcome_event_id": outcome.id,
                },
                truth_status=TruthStatus.INFERRED,
                provenance=change.provenance,
            )
            self.event_store.append(event)

    def _record_plugin_failure(
        self, subject_id: str, source_event: Event, plugin_name: str, error: Exception
    ) -> None:
        self.event_store.append(
            Event(
                subject_id=subject_id,
                kind=EventKind.AUDIT,
                content={
                    "plugin": plugin_name,
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
                truth_status=TruthStatus.OBSERVED,
                provenance=Provenance(
                    source="orchestrator",
                    source_event_ids=(source_event.id,),
                    method="plugin_failure_isolation",
                ),
            )
        )
