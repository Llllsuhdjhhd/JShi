"""工具使用：契约 + 可替换引擎 + 编排模块。

目录 / 包管理归引擎（如 Pi）。匠石侧不维护自己的工具注册表。
"""

from __future__ import annotations

from .contract import (
    AskMode,
    Budget,
    FeedbackKind,
    PermissionContext,
    ToolEstimate,
    ToolFeedback,
    ToolOrigin,
    ToolProgress,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from .hang import HangRecord, HangStore, NEED_MIN_CHARS, OPEN_LIST_CAP
from .port import ToolEngine, ToolModule, ToolRunner, ToolUseOutcome
from .stub import StubEngine
from .pi_engine import PiEngine

__all__ = [
    "AskMode",
    "Budget",
    "FeedbackKind",
    "HangRecord",
    "HangStore",
    "NEED_MIN_CHARS",
    "OPEN_LIST_CAP",
    "PermissionContext",
    "PiEngine",
    "StubEngine",
    "ToolEngine",
    "ToolEstimate",
    "ToolFeedback",
    "ToolModule",
    "ToolOrigin",
    "ToolProgress",
    "ToolRequest",
    "ToolResult",
    "ToolRunner",
    "ToolStatus",
    "ToolUseOutcome",
]
