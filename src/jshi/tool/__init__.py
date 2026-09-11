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
from .hang import (
    HangRecord,
    HangStore,
    NEED_MIN_CHARS,
    OPEN_LIST_CAP,
    is_stage_fact,
    rule_wrap_from_item,
)
from .catalog import load_catalog
from .intake import IntakeRecord, IntakeStore
from .plan import PlanFailure, RulePlanner
from .port import ToolEngine, ToolModule, ToolRunner, ToolUseOutcome
from .service import ToolService, VisibleToolItem
from .stub import StubEngine
from .pi_engine import PiEngine

__all__ = [
    "AskMode",
    "Budget",
    "FeedbackKind",
    "HangRecord",
    "HangStore",
    "IntakeRecord",
    "IntakeStore",
    "NEED_MIN_CHARS",
    "OPEN_LIST_CAP",
    "PermissionContext",
    "PiEngine",
    "PlanFailure",
    "RulePlanner",
    "StubEngine",
    "is_stage_fact",
    "load_catalog",
    "rule_wrap_from_item",
    "ToolEngine",
    "ToolEstimate",
    "ToolFeedback",
    "ToolModule",
    "ToolOrigin",
    "ToolProgress",
    "ToolRequest",
    "ToolResult",
    "ToolRunner",
    "ToolService",
    "ToolStatus",
    "ToolUseOutcome",
    "VisibleToolItem",
]
