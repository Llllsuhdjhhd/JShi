"""确定性占位引擎：给 pytest 与 demo。工具目录固定，反馈流 = estimate -> progress -> result。

用「引擎」而非匠石注册表：目录由引擎返回（可替换）。
"""

from __future__ import annotations

from .contract import (
    AskMode,
    FeedbackKind,
    ToolEstimate,
    ToolFeedback,
    ToolProgress,
    ToolRequest,
    ToolResult,
    ToolStatus,
)


class StubEngine:
    """确定性占位工具引擎。

    - ``list_templates`` 固定返回一个占位工具（默认 ``echo``）。
    - ``execute`` 依请求产出一条确定性的反馈流：estimate -> progress -> result。
      结果回显 ``request.need``（或 ``params``），用于验证契约与闭环。
    """

    name = "stub"

    def __init__(self, tool_name: str = "echo") -> None:
        self._tool_name = tool_name

    def list_templates(self) -> tuple[str, ...]:
        return (self._tool_name,)

    def execute(self, request: ToolRequest) -> tuple[ToolFeedback, ...]:
        tool = request.template or self._tool_name
        echo_value = str(
            request.params.get("value") if isinstance(request.params.get("value"), str) else request.need
        )

        estimate = ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.ESTIMATE,
            estimate=ToolEstimate(
                cost_est=0.0,
                time_est_ms=1,
                resource_est={"tokens": 1},
                benefit=f"用占位工具 {tool} 跑一次",
                downside="占位工具，无真实副作用",
                need_confirm=False,
            ),
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
        return (estimate, progress, result_fb)

    def iter_execute(self, request: ToolRequest):
        yield from self.execute(request)
