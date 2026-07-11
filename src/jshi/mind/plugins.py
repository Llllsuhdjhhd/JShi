from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping, Sequence

from jshi.core import (
    CandidateChange,
    Event,
    PluginContext,
    PluginManifest,
    Provenance,
)


class MindDomain(StrEnum):
    MEMORY = "memory"
    VALUES = "values"
    AESTHETICS = "aesthetics"
    CAPABILITY = "capability"
    RELATIONSHIP = "relationship"
    KNOWLEDGE = "knowledge"
    DELIBERATION = "deliberation"


class PlaceholderMindPlugin:
    """A stable seam for domain-specific algorithms that do not exist yet."""

    def __init__(
        self,
        domain: MindDomain,
        initial_context: Mapping[str, Any] | None = None,
    ) -> None:
        self.domain = domain
        self.initial_context = dict(initial_context or {})
        self._manifest = PluginManifest(
            name=f"mind.{domain.value}",
            version="0.1.0",
            capabilities=(domain.value,),
        )

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    def contribute(self, context: PluginContext) -> Mapping[str, Any]:
        return {
            "domain": self.domain.value,
            **self.initial_context,
        }

    def propose_changes(
        self, context: PluginContext, outcome: Event
    ) -> Sequence[CandidateChange]:
        if self.domain is not MindDomain.MEMORY:
            return ()
        return (
            CandidateChange(
                subject_id=context.subject_id,
                target="memory",
                operation="remember",
                value={"event_id": outcome.id, "kind": outcome.kind.value},
                provenance=Provenance(
                    source=self.manifest.name,
                    source_event_ids=(context.event.id, outcome.id),
                    method="placeholder",
                ),
                confidence=1.0,
                significance=0.1,
            ),
        )

    def healthcheck(self) -> bool:
        return True

    def initialize(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def export_state(self) -> Mapping[str, Any]:
        return {}

    def migrate(self, previous_version: str, state: Mapping[str, Any]) -> None:
        pass


def default_mind_plugins() -> tuple[PlaceholderMindPlugin, ...]:
    return tuple(PlaceholderMindPlugin(domain) for domain in MindDomain)
