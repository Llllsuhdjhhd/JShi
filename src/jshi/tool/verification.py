"""Adapter-observed evidence, separate from an assistant's completion claim."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


def check_file(path: str, *, base: Path, text: str | None = None,
               sha256: str = "") -> dict[str, Any]:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = base / target
    target = target.resolve()
    fact: dict[str, Any] = {"path": str(target), "check": "file"}
    try:
        if not target.is_file():
            return {**fact, "passed": False, "reason": "文件不存在"}
        size = target.stat().st_size
        fact["bytes"] = size
        if size > 8 * 1024 * 1024:
            return {**fact, "passed": False, "reason": "文件超出本次回读上限，未核验内容"}
        data = target.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        fact["sha256"] = digest
        if text is not None and data.decode("utf-8-sig").replace("\r\n", "\n") != text.replace("\r\n", "\n"):
            return {**fact, "passed": False, "reason": "实际文件内容与本次写入不一致"}
        if sha256 and digest != sha256:
            return {**fact, "passed": False, "reason": "实际文件校验值不一致"}
        return {**fact, "passed": True}
    except (OSError, UnicodeError, ValueError) as exc:
        return {**fact, "passed": False, "reason": f"文件未能核验（{type(exc).__name__}）"}


def assess_pi(calls: list[dict], *, base: Path, created: bool = False) -> dict[str, Any]:
    """Only completed RPC tool calls supply evidence; assistant prose is excluded.

    A file receipt verifies that artifact, not the semantic sufficiency of the
    whole request. Other output remains available for cognition without claiming
    that a generic nonempty string proves completion.
    """
    facts = []
    for call in calls:
        if call.get("error"):
            continue
        args = call.get("args") or {}
        if call.get("name") == "write" and isinstance(args.get("path"), str) and isinstance(args.get("content"), str):
            facts.append(check_file(args["path"], base=base, text=args["content"]))
        try:
            output = json.loads(call.get("output") or "")
        except (ValueError, TypeError):
            continue
        if not isinstance(output, Mapping):
            continue
        for artifact in output.get("artifacts", ()) if isinstance(output.get("artifacts"), list) else ():
            if (isinstance(artifact, Mapping) and isinstance(artifact.get("path"), str)
                    and isinstance(artifact.get("sha256"), str) and len(artifact["sha256"]) == 64):
                facts.append(check_file(artifact["path"], base=base, sha256=artifact["sha256"]))
    errors = any(call.get("error") for call in calls)
    verdict = "passed" if (created or facts) and all(f["passed"] for f in facts) and not errors else "unknown"
    if facts and any(not f["passed"] for f in facts):
        verdict = "partial" if any(f["passed"] for f in facts) else "failed"
    return {"kind": "verification", "verdict": verdict, "facts": facts,
            "reason": "已核对实际产物；内容是否满足整项需求仍需结合目标判断" if verdict == "passed"
            else "尚无足够的产物核验依据；执行输出保留供判断",
            "completed_calls": len(calls), "call_errors": errors,
            "execution_observed": any(c.get("substantive") and not c.get("error") for c in calls)}


def can_advance(result) -> bool:
    """Checked artifacts, or substantive adapter output, can feed a next step.

    Observed query output is not independent verification of the whole goal.
    Missing/mismatched artifacts always block advancement.
    """
    if any(row.get('kind') == 'supervision' and row.get('decision') != 'accept'
           for row in result.execution):
        return False
    value = assessment_of(result)
    accepted = any(row.get('kind') == 'supervision' and row.get('decision') == 'accept'
                   for row in result.execution)
    return value.get('verdict') == 'passed' or (
        value.get('verdict') == 'unknown' and (value.get('execution_observed') is True
                                             or accepted and value.get('completed_calls', 0) > 0)
        and not value.get('call_errors'))


def assessment_of(result) -> dict[str, Any]:
    assessment = next((dict(row) for row in reversed(result.execution)
                       if row.get("kind") == "verification"), None)
    return assessment or {"kind": "verification", "verdict": "unknown", "facts": [],
                          "reason": "此执行器未提供目标核验依据，不能仅凭返回文字确认完成"}


def assessment_note(result) -> str:
    value = assessment_of(result)
    names = {"passed": "产物核验通过", "partial": "产物部分核验通过", "failed": "产物核验未通过", "unknown": "目标完成尚未核验"}
    lines = [names.get(value.get("verdict"), names["unknown"]), str(value.get("reason") or "")]
    review = next((r for r in reversed(result.execution) if r.get('kind') == 'supervision'), None)
    if review:
        if review.get('decision') == 'accept' and value.get('verdict') == 'unknown':
            lines[0] = '实际执行输出已结合目标审查；本次无独立文件产物核验'
        lines.append('目标审查：' + str(review.get('summary', '')))
    for fact in value.get("facts", ()):
        lines.append(f"文件：{fact.get('path', '')}；" + ("已回读核对" if fact.get("passed") else str(fact.get("reason") or "未核验")))
    return "\n".join(filter(None, lines))
