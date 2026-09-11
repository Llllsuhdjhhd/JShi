"""确定性占位引擎：给 pytest 与 demo。工具目录固定，反馈流 = progress -> result。

报价由 200/205 写入记挂，引擎不 yield estimate。
"""

from __future__ import annotations

from .contract import (
    AskMode,
    FeedbackKind,
    ToolFeedback,
    ToolProgress,
    ToolRequest,
    ToolResult,
    ToolStatus,
)


class StubEngine:
    """确定性占位工具引擎。

    - ``list_templates`` / ``list_commands`` 固定返回一个占位工具（默认 ``echo``）。
    - ``execute`` 产出 progress -> result。结果回显 ``request.need``（或 ``params``）。
    """

    name = "stub"

    def __init__(self, tool_name: str = "echo") -> None:
        self._tool_name = tool_name

    def list_templates(self) -> tuple[str, ...]:
        return (self._tool_name,)

    def list_commands(self) -> tuple[dict[str, str], ...]:
        return ({"name": self._tool_name, "description": "回显 need，仅测试"},)

    def execute(self, request: ToolRequest) -> tuple[ToolFeedback, ...]:
        tool = request.command or request.template or self._tool_name
        echo_value = str(
            request.params.get("value") if isinstance(request.params.get("value"), str) else request.need
        )

        progress = ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.PROGRESS,
            progress=ToolProgress(
                stage="running",
                step="1/1",
                partial="",
                cost_used=0.0,
                time_used_ms=1,
                resource_used={"tokens": 1},
                note=f"stub {tool} 运行中",
            ),
        )
        ok = request.ask is not AskMode.PROPOSE_ONLY
        result_fb = ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.RESULT,
            result=ToolResult(
                status=ToolStatus.OK if ok else ToolStatus.REJECTED,
                result={"tool": tool, "echo": echo_value},
                summary=f"{tool}: {echo_value}",
                ideal=True,
                cost=0.0,
                time_ms=1,
                resource={"tokens": 1},
                execution=({"tool": tool, "step": "echo"},),
                error="" if ok else "propose_only 未获执行许可",
            ),
        )
        return (progress, result_fb)

    def iter_execute(self, request: ToolRequest, cancel=None):
        del cancel
        yield from self.execute(request)
