from __future__ import annotations

from typing import Protocol

from jshi.experienceledger import ContextViewState, ExperienceLedgerPort


class ActiveZonePort(Protocol):
    """阶段②：只从 16 读取既往 ContextViewState，不是整块活跃区。"""

    def load(self, subject_id: str) -> ContextViewState:
        """返回 16 的当前既往视图。不重切、不调整、不消费本轮输入。"""


class InProcessActiveZone:
    """最小实现：原样读取 16 的 current_context_view。"""

    name = "in-process-active-zone"

    def __init__(self, ledger: ExperienceLedgerPort) -> None:
        self._ledger = ledger

    def load(self, subject_id: str) -> ContextViewState:
        return self._ledger.current_context_view(subject_id)
