from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from jshi.models import ResponsePlan

RESPONSE_MODES = frozenset({"respond", "think", "ignore", "wait"})
VALID_CHANNELS = frozenset({"verbal", "embodied"})
SILENT_MODES = frozenset({"think", "ignore", "wait"})
RESPONSE_STATUSES = RESPONSE_MODES | VALID_CHANNELS


@dataclass(frozen=True)
class ResponseMarkResult:
    activity_id: str
    recommended: tuple[str, ...]
    final: tuple[str, ...]
    unknown: tuple[str, ...]
    illegal_channels: tuple[str, ...]
    missing_reason: bool
    missing_items: bool
    source: str
    reason: str
    items: tuple[tuple[str, str], ...]


class ResponseMarkPort(Protocol):
    def mark(
        self,
        subject_id: str,
        activity_id: str,
        response_plan: ResponsePlan,
        *,
        human_override: Sequence[str] | None = None,
    ) -> ResponseMarkResult: ...


def evaluate_response_plan(
    response_plan: ResponsePlan,
    *,
    human_override: Sequence[str] | None = None,
) -> ResponseMarkResult:
    """校验 mode × channel；未知值与非法组合忽略。不写库。"""
    recommended = tuple(
        dict.fromkeys(
            [
                response_plan.mode,
                *(item.channel for item in response_plan.items),
            ]
        )
    )
    unknown: list[str] = [
        status for status in recommended if status not in RESPONSE_STATUSES
    ]
    illegal: list[str] = []
    accepted: list[str] = []
    if response_plan.mode in RESPONSE_MODES:
        accepted.append(response_plan.mode)
    for item in response_plan.items:
        if item.channel not in VALID_CHANNELS:
            if item.channel not in unknown:
                unknown.append(item.channel)
            continue
        if item.channel == "verbal" and response_plan.mode in SILENT_MODES:
            illegal.append("verbal")
            continue
        if item.channel not in accepted:
            accepted.append(item.channel)

    missing_reason = (
        response_plan.mode in SILENT_MODES and not response_plan.reason.strip()
    )
    missing_items = response_plan.mode == "respond" and not any(
        item.channel in VALID_CHANNELS for item in response_plan.items
    )

    source = "model_recommended"
    if human_override is not None:
        source = "human_overridden"
        accepted = []
        silent_override = any(status in SILENT_MODES for status in human_override)
        for status in dict.fromkeys(human_override):
            if status not in RESPONSE_STATUSES:
                if status not in unknown:
                    unknown.append(status)
                continue
            if status == "verbal" and silent_override:
                if "verbal" not in illegal:
                    illegal.append("verbal")
                continue
            if status not in accepted:
                accepted.append(status)

    return ResponseMarkResult(
        activity_id="",
        recommended=recommended,
        final=tuple(accepted),
        unknown=tuple(unknown),
        illegal_channels=tuple(illegal),
        missing_reason=missing_reason,
        missing_items=missing_items,
        source=source,
        reason=response_plan.reason,
        items=tuple((item.channel, item.text) for item in response_plan.items),
    )
