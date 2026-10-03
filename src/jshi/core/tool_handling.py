"""Model reports use of material, not execution success."""
from typing import Any, Mapping

TOOL_HANDLING_SCHEMA = {
    "type": "array", "items": {"type": "object", "properties": {
        "task_id": {"type": "string"},
        "disposition": {"enum": ["answered", "deferred", "dismissed"]},
        "evidence": {"type": "string"}, "reason": {"type": "string"},
        "work_complete": {"type": "boolean"},
    }, "required": ["task_id", "disposition"]},
}


def parse_tool_handling(raw: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        task_id = str(item.get("task_id") or "").strip()
        disposition = item.get("disposition")
        if task_id and isinstance(disposition, str) and disposition in {"answered", "deferred", "dismissed"}:
            out[task_id] = {
                "task_id": task_id, "disposition": disposition,
                "evidence": str(item.get("evidence") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
                "work_complete": item.get("work_complete") is True,
            }
    return tuple(out.values())
