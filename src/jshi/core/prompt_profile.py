"""Validated JEV routing: prompt level, portrait view and history recall before assembly."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


MODULES = frozenset({"tool", "memory", "person"})


def valid_level(value, default=1):
    return value if type(value) is int and 1 <= value <= 3 else default


def normalize_hint(value):
    if not isinstance(value, Mapping) or valid_level(value.get("level"), 0) == 0:
        return {}
    needs = value.get("needs", [])
    if not isinstance(needs, list) or any(not isinstance(n, str) or n not in MODULES for n in needs):
        return {}
    if "recall_memory" in value and type(value["recall_memory"]) is not bool:
        return {}
    return {"level": value["level"], "needs": list(dict.fromkeys(needs)),
            **({"recall_memory": value["recall_memory"]} if "recall_memory" in value else {})}


@dataclass(frozen=True)
class PromptProfile:
    level: int = 1
    modules: tuple[str, ...] = ()
    recall_memory: bool = True

    @property
    def scene_view(self):
        return 4 - self.level

    def includes(self, module):
        return module == "person" or (module == "memory" and self.recall_memory) or (module == "tool" and self.level == 1)


def select_profile(input_ids, calls):
    """Only hints bound to all current inputs can reduce the full default.

    Calls for prior batches are diagnostic context, never routing authority.
    A merged batch takes the most complete level; optional modules cannot restore tools.
    """
    expected = set(input_ids)
    covered = set()
    hints = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        raw_ids = call.get("input_ids", ())
        if not isinstance(raw_ids, (list, tuple)) or any(not isinstance(i, str) for i in raw_ids):
            continue
        ids = set(raw_ids) & expected
        if not ids:
            continue
        hint = normalize_hint(call.get("main_prompt_hint"))
        if call.get("timed_out") or call.get("model_error"):
            # Failed routing does not justify an expensive speculative history
            # search. Keep full response/tool capability and existing scene/persons.
            hint = {"level": 1, "recall_memory": False}
        if hint:
            covered.update(ids)
            hints.append(hint)
    if not expected or covered != expected:
        return PromptProfile()
    return PromptProfile(min(h["level"] for h in hints), recall_memory=any(h.get("recall_memory", True) for h in hints))
