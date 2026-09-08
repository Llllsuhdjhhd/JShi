"""工具使用模块端口：可替换引擎 + 编排模块。

- ``ToolEngine``：工具使用引擎（可替换）。工具目录 / 包管理由引擎负责
  （Pi 用 ``get_commands`` / skills / packages），匠石侧不维护自己的注册表。
- ``ToolModule``：收 ``ToolRequest``，经引擎跑，返回反馈流 + 终态结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contract import FeedbackKind, ToolFeedback, ToolRequest, ToolResult, ToolStatus


class ToolEngine(Protocol):
    """工具使用引擎（可替换）。目录 / 安装 / 执行归引擎。"""

    name: str

    def list_templates(self) -> tuple[str, ...]:
        """返回可用工具模板清单。真接 Pi 后来自 ``get_commands`` / skills。"""
        ...

    def execute(self, request: ToolRequest) -> tuple[ToolFeedback, ...]:
        """执行一次工具使用，返回反馈流（estimate / progress / result）。

        引擎只产出候选结果与过程反馈，不写匠石长期状态（D-004）。
        """
        ...


@dataclass(frozen=True)
class ToolUseOutcome:
    """一次工具使用的完整结果。"""

    request_id: str
    feedback: tuple[ToolFeedback, ...]
    result: ToolResult

    @property
    def estimate(self) -> ToolFeedback | None:
        for item in self.feedback:
            if item.kind is FeedbackKind.ESTIMATE:
                return item
        return None

    @property
    def progress(self) -> tuple[ToolFeedback, ...]:
        return tuple(item for item in self.feedback if item.kind is FeedbackKind.PROGRESS)

    @property
    def result_feedback(self) -> ToolFeedback | None:
        for item in self.feedback:
            if item.kind is FeedbackKind.RESULT:
                return item
        return None


class ToolModule:
    """工具使用模块：收请求 → 引擎执行 → 返回反馈流与终态结果。"""

    def __init__(self, engine: ToolEngine) -> None:
        self.engine = engine

    @property
    def engine_name(self) -> str:
        return getattr(self.engine, "name", "unknown")

    def list_templates(self) -> tuple[str, ...]:
        return self.engine.list_templates()

    def submit(self, request: ToolRequest) -> ToolUseOutcome:
        feedback = self.engine.execute(request)
        terminal = next(
            (
                item.result
                for item in reversed(feedback)
                if item.kind is FeedbackKind.RESULT and item.result is not None
            ),
            None,
        )
        result = terminal or ToolResult(
            status=ToolStatus.FAILED, error="no terminal result feedback"
        )
        return ToolUseOutcome(
            request_id=request.request_id,
            feedback=tuple(feedback),
            result=result,
        )
