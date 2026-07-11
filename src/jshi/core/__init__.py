"""跨子系统共享的稳定契约。"""

from .contracts import (
    CandidateChange,
    Event,
    EventKind,
    Plugin,
    PluginContext,
    PluginManifest,
    Provenance,
    SubjectState,
    TruthStatus,
)

__all__ = [
    "CandidateChange",
    "Event",
    "EventKind",
    "Plugin",
    "PluginContext",
    "PluginManifest",
    "Provenance",
    "SubjectState",
    "TruthStatus",
]
