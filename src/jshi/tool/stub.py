"""确定性占位引擎：给 pytest 与 demo。工具目录固定，反馈流 = progress -> result。

报价由 200/205 写入记挂，引擎不 yield estimate。

**它不执行任何真实动作，只会回显需求**。所以它必须处处自曝身份：
目录里的每条都带 ``source="stub"`` 与「占位」字样，结果一律 ``ideal=False``
并带 ``ideal_note``。包装层据此不得把回显写成「已经拿到结果」——这条是硬要求：
占位产物被当成事实，会让下游对着一个不存在的结论继续说下去。
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
        # 造出来的工具会进这里，于是下一轮策划能检索到（验「造完可复用」）。
        self._created: list[dict[str, str]] = []

    def list_templates(self) -> tuple[str, ...]:
        return (self._tool_name,)

    def list_commands(self) -> tuple[dict[str, str], ...]:
        base = {
            "name": self._tool_name,
            "description": "占位引擎：只把 need 回显回来，不执行任何真实动作",
            "source": "stub",
        }
        return (base,) + tuple(self._created)

    def execute(self, request: ToolRequest) -> tuple[ToolFeedback, ...]:
        if request.create is not None:
            return self._execute_create(request)
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
                summary=f"{tool}: 占位回显「{echo_value}」（占位引擎，不是真实结果）",
                # 占位引擎从不真的办成事：ideal 必须为假，且把原因写清楚，
                # 好让包装层与调试视图都能看见「这一步没有真实产出」。
                ideal=False,
                ideal_note="占位引擎：只回显需求，未真实执行",
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

    def _execute_create(self, request: ToolRequest) -> tuple[ToolFeedback, ...]:
        """造工具：占位实现**只登记名字与意图**，不产生任何真实能力。

        登记是为了让「造完能再被检索到」这条链可测；登记出来的条目必须自曝是占位，
        否则下一轮策划会把它当成真工具去用。
        """
        spec = request.create
        assert spec is not None
        name = (spec.tool_name or "created_tool").strip()
        intent = (spec.tool_intent or request.need or "").strip()
        self._created.append(
            {
                "name": name,
                "description": f"占位实现（未真实创建）：{intent or '新造的工具'}",
                "source": "stub",
            }
        )
        progress = ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.PROGRESS,
            progress=ToolProgress(
                stage="start",
                step=name,
                partial="",
                note=f"stub 开始造工具 {name}",
            ),
        )
        result_fb = ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.RESULT,
            result=ToolResult(
                status=ToolStatus.OK,
                result={"created": name, "tool_intent": intent},
                summary=f"占位实现：只登记了 {name} 这个名字，没有真正创建工具",
                ideal=False,
                ideal_note="占位引擎：只登记名字，未真实创建",
                cost=0.0,
                time_ms=1,
                resource={"tokens": 1},
                execution=({"tool": name, "step": "create"},),
            ),
        )
        return (progress, result_fb)
