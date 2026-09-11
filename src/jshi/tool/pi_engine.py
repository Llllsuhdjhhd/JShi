"""Pi 引擎：用 Pi（pi.dev Coding Agent）作为工具执行引擎，走 RPC（JSONL over stdio）。

- 工具目录 / 包管理：由 Pi 负责（skills / extensions / ``pi list`` / ``get_commands``）。
  匠石侧**不建自己的工具注册表**，只通过本引擎查目录、驱动执行。
- 对齐：《doc/design/工具使用.md》——Pi 只局限在工具模块内；报价由 200/205 写，
  本引擎只译事件、报真实进度。D-004：不写匠石长期状态。
- 前置：需装 Pi 并配置 provider / 模型。主流程默认仍 Stub；``JSHI_TOOL_ENGINE=pi`` 才接本引擎。
- RPC 用严格 LF 分隔的 JSONL。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any, Iterator, Mapping

from .contract import (
    FeedbackKind,
    ToolFeedback,
    ToolProgress,
    ToolRequest,
    ToolResult,
    ToolStatus,
)


class PiEngine:
    """把 ``ToolRequest`` 翻译成 Pi RPC 的一次 prompt，把 ``tool_execution_*``
    事件翻译成 ``ToolFeedback``。不 yield estimate。"""

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
        list_timeout_s: float = 5.0,
    ) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.executable = executable
        self.timeout_s = timeout_s
        self.list_timeout_s = list_timeout_s
        self.extra_args = tuple(extra_args)

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
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    @staticmethod
    def _send(proc: subprocess.Popen, obj: Mapping[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass

    def _arm_watchdog(
        self,
        proc: subprocess.Popen,
        *,
        cancel: threading.Event | None = None,
        timeout_s: float | None = None,
    ) -> tuple[threading.Event, dict[str, str]]:
        """墙钟超时或 cancel 时杀掉子进程，让 stdout 读循环退出。"""
        stop = threading.Event()
        reason: dict[str, str] = {"stop": ""}
        limit = self.timeout_s if timeout_s is None else timeout_s

        def watch() -> None:
            deadline = time.monotonic() + max(0.05, limit)
            while not stop.wait(0.05):
                if proc.poll() is not None:
                    return
                if cancel is not None and cancel.is_set():
                    reason["stop"] = "cancelled"
                    self._kill(proc)
                    return
                if time.monotonic() >= deadline:
                    reason["stop"] = "timeout"
                    self._kill(proc)
                    return

        thread = threading.Thread(target=watch, name="pi-watchdog", daemon=True)
        thread.start()
        return stop, reason

    def _read_events(
        self, proc: subprocess.Popen, timeout_s: float | None = None
    ) -> list[Mapping[str, Any]]:
        stop, _reason = self._arm_watchdog(proc, timeout_s=timeout_s)
        events: list[Mapping[str, Any]] = []
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        finally:
            stop.set()
        return events

    def list_templates(self) -> tuple[str, ...]:
        return tuple(item["name"] for item in self.list_commands())

    def list_commands(self) -> tuple[Mapping[str, str], ...]:
        """``get_commands``：name + description。失败或超时返回空，205 仍可读匠石槽位。"""
        try:
            proc = self._spawn()
        except OSError:
            return ()
        try:
            self._send(proc, {"id": "list", "type": "get_commands"})
            events = self._read_events(proc, timeout_s=self.list_timeout_s)
        except Exception:
            return ()
        finally:
            self._close(proc)
        found: list[Mapping[str, str]] = []
        for event in events:
            if event.get("type") != "response" or event.get("command") != "get_commands":
                continue
            data = event.get("data") or {}
            for cmd in data.get("commands") or ():
                if not isinstance(cmd, Mapping):
                    continue
                name = str(cmd.get("name") or "").strip()
                if not name:
                    continue
                description = str(
                    cmd.get("description") or cmd.get("detail") or ""
                ).strip()
                found.append({"name": name, "description": description})
        return tuple(found)

    def execute(self, request: ToolRequest) -> tuple[ToolFeedback, ...]:
        return tuple(self.iter_execute(request))

    def iter_execute(
        self,
        request: ToolRequest,
        cancel: threading.Event | None = None,
    ) -> Iterator[ToolFeedback]:
        """边读边交出反馈。未映射事件丢掉。不 yield estimate。"""
        prompt = self._build_prompt(request)
        proc = self._spawn()
        saw_result = False
        stop, reason = self._arm_watchdog(proc, cancel=cancel)
        try:
            self._send(proc, {"id": request.request_id, "type": "prompt", "message": prompt})
            assert proc.stdout is not None
            for raw in proc.stdout:
                if cancel is not None and cancel.is_set():
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                converted = self._convert_event(request, event)
                if converted is not None:
                    if converted.kind is FeedbackKind.RESULT:
                        saw_result = True
                    yield converted
                if event.get("type") == "agent_settled":
                    break
        finally:
            stop.set()
            self._close(proc)

        if saw_result:
            return
        if reason.get("stop") == "cancelled" or (cancel is not None and cancel.is_set()):
            yield ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(status=ToolStatus.ABORTED, error="已取消"),
            )
            return
        if reason.get("stop") == "timeout":
            yield ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(status=ToolStatus.FAILED, error="工具执行超时"),
            )
            return
        yield ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.RESULT,
            result=ToolResult(status=ToolStatus.FAILED, error="Pi 会话未产出工具结果"),
        )

    def _build_prompt(self, request: ToolRequest) -> str:
        params = json.dumps(request.params, ensure_ascii=False) if request.params else "{}"
        command = (request.command or "").strip() or "（由你选用）"
        field_ref = (
            json.dumps(dict(request.field_ref), ensure_ascii=False)
            if request.field_ref
            else "（无）"
        )
        return (
            f"请使用工具 {command} 完成下面的需求，"
            f"参数：{params}。\n\n需求：{request.need}\n"
            f"期望结果：{request.expected_result or '（未指定）'}\n"
            f"现场引用：{field_ref}"
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
            proc.kill()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
