from __future__ import annotations

import random
from typing import Protocol, Sequence, TypeVar

T = TypeVar("T")


class ChancePolicy(Protocol):
    def select_attention(self, candidates: Sequence[T], limit: int) -> tuple[T, ...]: ...


class NoChancePolicy:
    def select_attention(self, candidates: Sequence[T], limit: int) -> tuple[T, ...]:
        return tuple(candidates[:limit])


class SeededAttentionPolicy:
    """Reproducible low-risk exploration; it only selects candidates."""

    def __init__(self, seed: int | None = None) -> None:
        self._random = random.Random(seed)

    def select_attention(self, candidates: Sequence[T], limit: int) -> tuple[T, ...]:
        if limit <= 0 or not candidates:
            return ()
        count = min(limit, len(candidates))
        return tuple(self._random.sample(list(candidates), count))
