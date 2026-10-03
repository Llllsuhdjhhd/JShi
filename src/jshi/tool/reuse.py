"""Opt-in rules for read-only tools. Unknown/incomplete coverage never hits cache."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from .contract import FeedbackKind, ToolRequest, ToolStatus


def normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value).strip()
    if isinstance(value, Mapping):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    return value


@dataclass(frozen=True)
class ReusePolicy:
    # Declared by application/tool owner, never accepted from a model response.
    ttl_s: float
    required_params: tuple[str, ...]
    # Optional inclusive date range. All other parameters must match exactly.
    date_range: tuple[str, str] | None = None

    def covers(self, stored: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
        have, need = normalize(stored), normalize(wanted)
        if any(need.get(key) in (None, "", []) for key in self.required_params):
            return False
        if self.date_range:
            start, end = self.date_range
            try:
                a, b = date.fromisoformat(have[start]), date.fromisoformat(have[end])
                c, d = date.fromisoformat(need[start]), date.fromisoformat(need[end])
            except (KeyError, TypeError, ValueError):
                return False
            if not a <= c <= d <= b:
                return False
            for key in (start, end):
                have.pop(key, None)
                need.pop(key, None)
        return have == need

    def match(self, hang: Any, request: ToolRequest, now: datetime) -> bool:
        terminal = next((f for f in reversed(hang.feedback)
                         if f.kind is FeedbackKind.RESULT and f.result is not None), None)
        if terminal is None or terminal.result.status is not ToolStatus.OK:
            return False
        result = terminal.result
        # Engine success alone does not prove requested coverage. Tool must supply
        # actual coverage separately from the request parameters.
        coverage = result.result.get("coverage")
        if not isinstance(coverage, Mapping) or not result.ideal:
            return False
        age = (now - terminal.at).total_seconds()
        return (0 <= age <= self.ttl_s and self.ttl_s > 0
                and self.covers(hang.params, request.params)
                and self.covers(coverage, request.params)
                and bool(hang.summary.strip()))
