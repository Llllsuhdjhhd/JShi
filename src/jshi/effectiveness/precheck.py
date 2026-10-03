"""建议预检。通过也不写入。本阶段没有采纳。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from jshi.core.params import REGISTRY, ZONE_FLUSH_CONSTRAINT, ParamSpec

from .counters import COUNTERS


@dataclass(frozen=True)
class Change:
    name: str
    old: Any
    new: Any
    store: str = "overlay"


@dataclass(frozen=True)
class Proposal:
    counter_id: str
    sample_activity_ids: tuple[str, ...]
    cause: str
    window_start: str
    window_end: str
    proposed: Change | None
    companion: Change | None = None
    current: dict[str, Any] | None = None


@dataclass(frozen=True)
class PrecheckResult:
    ok: bool
    reason: str = ""
    companion: Change | None = None
    constraint: str = ""


def _spec(name: str) -> ParamSpec | None:
    return REGISTRY.get(name)


def _in_range(spec: ParamSpec, value: Any) -> str:
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return "新值必须是整数"
    if spec.low is not None and value < spec.low:
        return f"小于允许范围 {spec.low}"
    if spec.high is not None and value > spec.high:
        return f"大于允许范围 {spec.high}"
    return ""


def _number(current: dict[str, Any], name: str, default: int) -> int:
    value = current.get(name, default)
    return int(value)


def _constraint_companion(
    proposed: Change,
    companion: Change | None,
    current: dict[str, Any],
) -> tuple[str, Change | None, str]:
    """返回 (拒绝理由, 应附带的绑定项, 约束名)。理由为空表示约束通过。"""
    rule = ZONE_FLUSH_CONSTRAINT
    names = {proposed.name, companion.name if companion else ""}
    if not names & {rule.left, rule.right}:
        if companion is not None:
            return "未登记的关联不能同时改第二项", None, ""
        return "", None, ""
    flush = _number(
        current, rule.right, int(REGISTRY[rule.right].default or 0)
    )
    zone = _number(current, rule.left, int(REGISTRY[rule.left].default or 0))
    if proposed.name == rule.right:
        flush = int(proposed.new)
    elif proposed.name == rule.left:
        zone = int(proposed.new)
    if companion is not None:
        if companion.name == rule.right:
            flush = int(companion.new)
        elif companion.name == rule.left:
            zone = int(companion.new)
    needed_zone = math.ceil(flush * rule.ratio)
    if zone >= needed_zone:
        if companion is not None:
            return "约束未被破坏，不能随同改第二项", None, rule.name
        return "", None, ""
    if proposed.name == rule.right:
        expected = Change(
            name=rule.left,
            old=_number(current, rule.left, int(REGISTRY[rule.left].default or 0)),
            new=needed_zone,
            store="overlay",
        )
    elif proposed.name == rule.left:
        needed_flush = math.floor(zone / rule.ratio)
        expected = Change(
            name=rule.right,
            old=_number(current, rule.right, int(REGISTRY[rule.right].default or 0)),
            new=needed_flush,
            store="overlay",
        )
    else:
        return "未登记的关联被破坏", None, rule.name
    if companion is None:
        return "", expected, rule.name
    if companion.name != expected.name or companion.new != expected.new:
        return "绑定项必须刚好调整到满足约束，不能多调", None, rule.name
    return "", expected, rule.name


def precheck(proposal: Proposal) -> PrecheckResult:
    if not proposal.counter_id or proposal.counter_id not in COUNTERS:
        return PrecheckResult(False, "没有已登记的计数器")
    if not proposal.sample_activity_ids:
        return PrecheckResult(False, "没有样例")
    if not (proposal.cause or "").strip():
        return PrecheckResult(False, "没有原因")
    if not proposal.window_start or not proposal.window_end:
        return PrecheckResult(False, "没有计数窗口")
    if proposal.window_start > proposal.window_end:
        return PrecheckResult(False, "计数窗口起止颠倒")
    if proposal.proposed is None:
        return PrecheckResult(False, "没有改动")
    proposed = proposal.proposed
    companion = proposal.companion
    if (
        proposed.name in {"recall.default_level", "recall.limit"}
        and companion is not None
        and companion.name in {"recall.default_level", "recall.limit"}
        and proposed.name != companion.name
    ):
        return PrecheckResult(False, "同一次不能既改档位又改条数")
    if proposed.store == "overlay" and proposed.name.startswith("recall."):
        return PrecheckResult(False, "召回项不写入覆盖层")
    spec = _spec(proposed.name)
    if spec is None:
        return PrecheckResult(False, "参数未登记")
    range_error = _in_range(spec, proposed.new)
    if range_error:
        return PrecheckResult(False, range_error)
    if spec.level != "auto":
        level_reason = f"可调级别是 {spec.level}，本阶段不能自动改"
    else:
        level_reason = ""
    current = dict(proposal.current or {})
    reason, expected, constraint = _constraint_companion(proposed, companion, current)
    if reason:
        return PrecheckResult(False, reason, constraint=constraint)
    if companion is not None and expected is None:
        return PrecheckResult(False, "一次只能改一项")
    if level_reason:
        return PrecheckResult(
            False, level_reason, companion=expected, constraint=constraint
        )
    if expected is not None and companion is None:
        return PrecheckResult(True, "", companion=expected, constraint=constraint)
    return PrecheckResult(True, "", companion=expected, constraint=constraint)
