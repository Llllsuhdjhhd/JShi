"""Pi 引擎：用 Pi（pi.dev Coding Agent）作为工具执行引擎，走 RPC（JSONL over stdio）。

- 工具目录 / 包管理：由 Pi 负责（skills / extensions / ``pi list`` / ``get_commands``）。
  匠石侧**不建自己的工具注册表**，只通过本引擎查目录、驱动执行。
- 对齐：《doc/design/工具使用.md》§6.1「借引擎，不借循环」——Pi 只局限在工具模块内，
  匠石主循环与契约不变；D-004：只出候选结果 + 过程反馈，不写匠石长期状态。
- 前置：需装 Pi（``npm install -g --ignore-scripts @earendil-works/pi-coding-agent``）
  并配置 provider / 模型。本适配为 **live 验证**，未在单元测试覆盖（单测用 StubEngine）。
- 注意：RPC 用严格 LF 分隔的 JSONL；请勿用会把 Unicode 分隔符当换行的通用行读取器。
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Mapping

from .contract import (
    AskMode,
    Budget,
    FeedbackKind,
    ToolEstimate,
    ToolFeedback,
    ToolProgress,
    ToolRequest,
    ToolResult,
    ToolStatus,
)


class PiEngine:
    """把 ``ToolRequest`` 翻译成 Pi RPC 的一次 prompt，把 ``tool_execution_*``
    事件翻译成 ``ToolFeedback``，最终回灌。"""

    name = "pi"

    def __init__(
        self,
        *,
        provider: str = "",
        model: str = "",
        api_key: str = "",
        executable: str = "pi",
        timeout_s: float = 120.0,
        extra_args: tuple[str, ...] = (),
    ) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.executable = executable
        self.timeout_s = timeout_s
        self.extra_args = tuple(extra_args)

    # ------------------------------------------------------------------
    # 进程与协议
    # ------------------------------------------------------------------

    def _base_args(self) -> list[str]:
        args = [self.executable, "--mode", "rpc", "--no-session"]
        if self.provider:
            args += ["--provider", self.provider]
        if self.model:
            args += ["--model", self.model]
        if self.api_key:
            args += ["--api-key", self.api_key]
        args += list(self.extra_args)
        return args

    def _spawn(self) -> subprocess.Popen:
        return subprocess.Popen(
            self._base_args(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    @staticmethod
    def _send(proc: subprocess.Popen, obj: Mapping[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _read_events(self, proc: subprocess.Popen) -> "list[Mapping[str, Any]]":
        events: list[Mapping[str, Any]] = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------

    def list_templates(self) -> tuple[str, ...]:
        """向 Pi 查一次 ``get_commands``，取 skill / extension 命令名作为工具模板清单。

        Pi 未装或不可用时抛出异常（调用方决定降级到 StubEngine 或报错）。
        """
        proc = self._spawn()
        try:
            self._send(proc, {"id": "list", "type": "get_commands"})
            events = self._read_events(proc)
        finally:
            self._close(proc)
        names: list[str] = []
        for event in events:
            if event.get("type") != "response" or event.get("command") != "get_commands":
                continue
            data = event.get("data") or {}
            for cmd in data.get("commands") or ():
                name = cmd.get("name")
                if name:
                    names.append(name)
        return tuple(names)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def execute(self, request: ToolRequest) -> tuple[ToolFeedback, ...]:
        """向 Pi 发一次 prompt，令其用 ``request.template`` 工具满足 ``request.need``。

        返回反馈流：synthetic estimate -> 由 ``tool_execution_*`` 事件转出的
        progress / result。结果只作候选反馈，不写长期状态。
        """
        estimate = ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.ESTIMATE,
            estimate=ToolEstimate(
                benefit=f"用 Pi 工具 {request.template or '(auto)'} 满足：{request.need}",
                downside="Pi 为 LLM 驱动，成本 / 时间取决于模型",
                need_confirm=request.permission.require_confirm,
            ),
        )

        prompt = self._build_prompt(request)
        proc = self._spawn()
        feedback: list[ToolFeedback] = [estimate]
        try:
            self._send(proc, {"id": request.request_id, "type": "prompt", "message": prompt})
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                converted = self._convert_event(request, event)
                if converted is not None:
                    feedback.append(converted)
                if event.get("type") == "agent_settled":
                    break
        finally:
            self._close(proc)

        if not any(item.kind is FeedbackKind.RESULT for item in feedback):
            feedback.append(
                ToolFeedback(
                    request_id=request.request_id,
                    kind=FeedbackKind.RESULT,
                    result=ToolResult(
                        status=ToolStatus.FAILED, error="Pi 会话未产出工具结果"
                    ),
                )
            )
        return tuple(feedback)

    def _build_prompt(self, request: ToolRequest) -> str:
        params = json.dumps(request.params, ensure_ascii=False) if request.params else "{}"
        return (
            f"请使用工具模板 {request.template or '（由你选用）'} 完成下面的需求，"
            f"参数：{params}。\n\n需求：{request.need}\n"
            f"期望结果：{request.expected_result or '（未指定）'}"
        )

    def _convert_event(
        self, request: ToolRequest, event: Mapping[str, Any]
    ) -> ToolFeedback | None:
        event_type = event.get("type")
        if event_type == "tool_execution_start":
            return ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.PROGRESS,
                progress=ToolProgress(
                    stage="start",
                    step=str(event.get("toolName") or ""),
                    partial="",
                    note=f"开始工具 {event.get('toolName')}",
                ),
            )
        if event_type == "tool_execution_update":
            partial = event.get("partialResult") or {}
            text = self._content_text(partial.get("content"))
            return ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.PROGRESS,
                progress=ToolProgress(
                    stage="running",
                    step=str(event.get("toolName") or ""),
                    partial=text,
                    note="工具执行中",
                ),
            )
        if event_type == "tool_execution_end":
            result = event.get("result") or {}
            is_error = bool(event.get("isError"))
            content = self._content_text(result.get("content"))
            status = ToolStatus.FAILED if is_error else ToolStatus.OK
            return ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(
                    status=status,
                    result={"content": content},
                    summary=(content or "").strip()[:200],
                    ideal=not is_error,
                    ideal_note="" if not is_error else f"工具报错：{content}",
                    execution=(
                        {
                            "tool": event.get("toolName"),
                            "toolCallId": event.get("toolCallId"),
                            "raw": dict(result),
                        },
                    ),
                    error="" if not is_error else content,
                ),
            )
        return None

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            return "\n".join(parts)
        if isinstance(content, Mapping):
            return str(content.get("text") or "")
        return ""

    @staticmethod
    def _close(proc: subprocess.Popen) -> None:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
