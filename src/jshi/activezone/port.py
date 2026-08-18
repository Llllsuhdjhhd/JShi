from __future__ import annotations

from typing import Protocol

from jshi.experienceledger import ContextViewState, ExperienceLedgerPort


class ActiveZonePort(Protocol):
    """活跃区：只从 16 读取当前 ContextViewState。"""

    def load(
        self,
        subject_id: str,
        input_text: str,
        *,
        limit_chars: int | None = None,
    ) -> ContextViewState:
        """返回 16 的当前上下文视图。不重切、不调整。"""


class InProcessActiveZone:
    """最小实现：原样读取 16 的 current_context_view。"""

    name = "in-process-active-zone"

    def __init__(self, ledger: ExperienceLedgerPort) -> None:
        self._ledger = ledger

    def load(
        self,
        subject_id: str,
        input_text: str,
        *,
        limit_chars: int | None = None,
    ) -> ContextViewState:
        del input_text, limit_chars
        return self._ledger.current_context_view(subject_id)
