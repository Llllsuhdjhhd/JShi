from __future__ import annotations

from typing import Protocol, TypeVar

T = TypeVar("T")


class ChancePort(Protocol):
    """受约束偶然性：在事实、价值边界、承诺与情境约束内影响注意/联想/探索。"""

    def apply(self, stage: str, payload: T) -> T: ...


class PlaceholderChance:
    """占位实现：原样返回，不引入偶然性。"""

    name = "placeholder-chance"

    def apply(self, stage: str, payload: T) -> T:
        return payload
