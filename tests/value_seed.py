"""测试里给 100 价值库灌条目，不走 add_personal_item。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def accepted_value(content: str, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "role": "value",
        "content": content,
        "source_type": "classic_work",
        "status": "accepted",
    }
    entry.update(fields)
    return entry


def accepted_boundary(content: str, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "role": "boundary",
        "content": content,
        "source_type": "classic_work",
        "status": "accepted",
        "binding": True,
    }
    entry.update(fields)
    return entry


def import_values(host, subject_id: str, entries: Sequence[Mapping[str, Any]]):
    values = host if hasattr(host, "import_entries") else host.values
    return values.import_entries(subject_id, entries)
