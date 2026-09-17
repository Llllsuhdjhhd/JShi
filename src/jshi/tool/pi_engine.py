"""Pi 引擎：用 Pi（pi.dev Coding Agent）作为工具执行引擎，走 RPC（JSONL over stdio）。

- 工具目录 / 包管理：由 Pi 负责（skills / extensions / ``pi list`` / ``get_commands``）。
  匠石侧**不建自己的工具注册表**，只通过本引擎查目录、驱动执行。
- 对齐：《doc/design/工具使用.md》——Pi 只局限在工具模块内；报价由 200/205 写，
  本引擎只译事件、报真实进度。D-004：不写匠石长期状态。
- 前置：需装 Pi 并配置 provider / 模型。**主流程默认接本引擎**；
  ``JSHI_TOOL_ENGINE=stub`` 才回落到占位引擎（测试 / 离线演示用）。
- RPC 用严格 LF 分隔的 JSONL。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .contract import (
    FeedbackKind,
    ToolFeedback,
    ToolProgress,
    ToolRequest,
    ToolResult,
    ToolStatus,
)

logger = logging.getLogger(__name__)

# 脚本类工具的成功输出优先作为整轮 RESULT（读文件 / ls 不当终态）。
_SHELL_TOOLS = frozenset({"bash", "powershell", "shell", "cmd", "pwsh"})


def _shell_output_is_exploration(text: str) -> bool:
    """探路输出：列 skills 目录、读 SKILL.md 等。不得占满整轮 RESULT 摘要。"""
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower()
    if "rate=" in low or '"query"' in t or '"results"' in t:
        return False
    if t.lstrip().startswith("---") and re.search(
        r"(?m)^name:\s*\S+", t[:400]
    ):
        return True
    if "能力意图" in t and ("参数" in t or "参数格式" in t):
        return True
    if re.search(r"[\\/]skills[\\/]", t, re.I) and re.search(
        r"(?i)\.(ps1|sh|json|md)\b", t
    ):
        return True
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if 1 <= len(lines) <= 15:
        fileish = 0
        for ln in lines:
            if (
                ln.endswith(":")
                or re.fullmatch(r"[\w.\-]+", ln) is not None
                or re.search(r"(?i)\.(ps1|sh|json|md|py)$", ln)
            ):
                fileish += 1
        if fileish == len(lines) and "{" not in t:
            return True
    return False


def _join_shell_result_parts(parts: list[str]) -> str:
    """多段 bash 成功输出合并；丢掉探路噪声，避免截断后只剩目录清单。"""
    useful = [p.strip() for p in parts if not _shell_output_is_exploration(p)]
    return "\n".join(useful).strip()


def _compact_shell_summary(parts: list[str], *, limit: int = 1500) -> str:
    """给 RESULT.summary / 包装器用的短摘要：多段实质输出各留一段，避免只剩第一段检索 JSON。"""
    useful = [p.strip() for p in parts if p and not _shell_output_is_exploration(p)]
    if not useful:
        return ""
    if len(useful) == 1:
        text = useful[0]
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    # 各段均分预算，汇率行与检索 JSON 都能进包装器。
    per = max(120, (limit - 8 * (len(useful) - 1)) // len(useful))
    chunks: list[str] = []
    for part in useful:
        if len(part) <= per:
            chunks.append(part)
        else:
            chunks.append(part[: per - 1].rstrip() + "…")
    joined = "\n---\n".join(chunks)
    if len(joined) <= limit:
        return joined
    return joined[: limit - 1].rstrip() + "…"


def _skill_name(raw: str) -> str:
    """把模型给的造工具名规范成 Pi skill name（小写、数字、连字符）。"""
    value = (raw or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:64] or "generated-tool"


def _usage_from_event(event: Mapping[str, Any]) -> Mapping[str, Any]:
    candidates = (
        event.get("usage"),
        (event.get("result") or {}).get("usage"),
        (event.get("data") or {}).get("usage"),
    )
    for candidate in candidates:
        if isinstance(candidate, Mapping) and candidate:
            return candidate
    return {}


def _merge_usage(
    current: Mapping[str, Any], incoming: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Pi 顶层 usage 是累计值，直接取最新一份即可。"""
    return dict(incoming) if incoming else dict(current)


def _usage_cost(usage: Mapping[str, Any]) -> float | None:
    cost = usage.get("cost")
    if isinstance(cost, Mapping):
        value = cost.get("total")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _usage_resource(usage: Mapping[str, Any]) -> Mapping[str, float]:
    out: dict[str, float] = {}
    if isinstance(usage, Mapping):
        for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = float(value)
    return out


def _tool_call_key(event: Mapping[str, Any], fallback_index: int) -> str:
    call_id = str(event.get("toolCallId") or "").strip()
    if call_id:
        return call_id
    tool_name = str(event.get("toolName") or "").strip()
    return f"{tool_name or 'tool'}:{fallback_index}"


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
        home_dir: str | Path | None = None,
        env_api_key_name: str = "DEEPSEEK_API_KEY",
    ) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.executable = executable
        self.timeout_s = timeout_s
        self.list_timeout_s = list_timeout_s
        self.extra_args = tuple(extra_args)
        # Pi 的配置 / session 落点。默认塞进仓库 `.tmp/pi`：Pi 要写 settings 锁、
        # auth、sessions，落在用户目录下会被沙箱拒（EPERM），也会污染本机配置。
        self.home_dir = Path(home_dir) if home_dir else Path(".tmp/pi")
        self.env_api_key_name = env_api_key_name
        self._last_stop = ""
        self._spawn_error = ""
        # home_dir 不再默认相对 CWD：cli 传 None 时也落到仓库工作区内的绝对路径，
        # 避免换目录启动后写到工作区外撞沙箱。
        self.home_dir = (
            Path(home_dir).expanduser().resolve()
            if home_dir is not None
            else (Path.cwd() / ".tmp" / "pi").resolve()
        )
        self._agent_dir.mkdir(parents=True, exist_ok=True)
        self._session_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _agent_dir(self) -> Path:
        return self.home_dir / "agent"

    @property
    def _session_dir(self) -> Path:
        return self.home_dir / "sessions"

    def _base_args(self) -> list[str]:
        args = [self.executable, "--mode", "rpc", "--no-session"]
        if self.provider:
            args += ["--provider", self.provider]
        if self.model:
            args += ["--model", self.model]
        if self.api_key:
            args += ["--api-key", self.api_key]
        args += ["--session-dir", str(self._session_dir)]
        args += list(self.extra_args)
        return args

    def _child_env(self) -> Mapping[str, str]:
        """子进程环境：把 Pi 的落点挪进工作区，并让 provider 拿得到 key。"""
        env = dict(os.environ)
        env["PI_CODING_AGENT_DIR"] = str(self._agent_dir)
        env["PI_CODING_AGENT_SESSION_DIR"] = str(self._session_dir)
        env.setdefault("PI_OFFLINE", "1")
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        key = self.api_key or os.getenv("JSHI_MODEL_API_KEY", "")
        if key and self.env_api_key_name:
            env.setdefault(self.env_api_key_name, key)
        return env

    def _spawn(self) -> subprocess.Popen:
        args = self._base_args()
        # Windows 上 npm 只生成 pi.cmd。CreateProcess 不能直接执行 .cmd，
        # 因此 .cmd/.bat 走 cmd shell 启动，避免 PermissionError。
        suffix = Path(self.executable).suffix.lower()
        if os.name == "nt" and suffix in {".cmd", ".bat"}:
            return subprocess.Popen(
                subprocess.list2cmdline(args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=self._child_env(),
                shell=True,
            )
        return subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=self._child_env(),
        )

    @staticmethod
    def _send(proc: subprocess.Popen, obj: Mapping[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        """杀**整棵进程树**。

        Windows 上 ``pi`` 常是 ``pi.cmd``（cmd.exe 包着 node）：只杀直接子进程会留下
        孙进程握着 stdout，读循环永远等不到 EOF。所以先 taskkill /T，再兜底 kill。
        """
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
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

    def _iter_events(
        self,
        proc: subprocess.Popen,
        timeout_s: float | None = None,
        *,
        stop_on_command: str = "",
        stop_on_settled: bool = False,
        cancel: threading.Event | None = None,
    ) -> Iterator[Mapping[str, Any]]:
        """边读边交出事件；**不依赖杀进程也能停**。

        后台线程只负责读行放进队列，主循环按墙钟 deadline 取；超时/取消/收到
        目标应答即返回。这样即使子进程（Windows 上 ``pi.cmd`` → node）杀不干净，
        调用方也一定会在 deadline 内收回，不会被卡死。

        结束原因写在 ``self._last_stop``（`timeout` / `cancelled` / 空），供调用方决定终态。
        """
        stop, reason = self._arm_watchdog(proc, cancel=cancel, timeout_s=timeout_s)
        self._last_stop = ""
        limit = self.timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + max(0.05, limit)
        lines: "queue.Queue[object]" = queue.Queue()
        sentinel = object()

        def pump() -> None:
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    lines.put(raw)
            except Exception:
                pass
            finally:
                lines.put(sentinel)

        threading.Thread(target=pump, name="pi-stdout", daemon=True).start()
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    self._last_stop = "cancelled"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._last_stop = "timeout"
                    break
                try:
                    raw = lines.get(timeout=min(0.5, remaining))
                except queue.Empty:
                    continue
                if raw is sentinel:
                    break
                line = str(raw).strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                yield event
                if (
                    stop_on_command
                    and event.get("type") == "response"
                    and event.get("command") == stop_on_command
                ):
                    break
                if stop_on_settled and event.get("type") == "agent_settled":
                    break
        finally:
            self._last_stop = self._last_stop or str(reason.get("stop") or "")
            stop.set()

    def _read_events(
        self,
        proc: subprocess.Popen,
        timeout_s: float | None = None,
        *,
        stop_on_command: str = "",
    ) -> list[Mapping[str, Any]]:
        """读事件行到列表（用于 ``get_commands`` 这类一次性应答）。"""
        return list(
            self._iter_events(
                proc, timeout_s=timeout_s, stop_on_command=stop_on_command
            )
        )

    def list_templates(self) -> tuple[str, ...]:
        return tuple(item["name"] for item in self.list_commands())

    def _skill_metadata(self, path: str) -> Mapping[str, Any]:
        """读 skill 目录里的 ``tool.json``，让 205 能拿回创建时的规格。"""
        if not path:
            return {}
        try:
            base = Path(path)
            candidate = base.with_name("tool.json") if base.is_file() else base / "tool.json"
            if not candidate.is_file():
                return {}
            data = json.loads(candidate.read_text(encoding="utf-8"))
            return data if isinstance(data, Mapping) else {}
        except Exception as exc:
            logger.warning("读取 Pi skill tool.json 失败：%s", exc)
            return {}

    def list_commands(self) -> tuple[Mapping[str, Any], ...]:
        """``get_commands``：name + description（+ source / path / params / meta）。

        失败、超时或缺可执行文件时返回空，205 仍可只读匠石槽位。
        Windows 上只有 ``pi.cmd`` 时，``executable`` 要写成 ``pi.cmd``。
        """
        try:
            proc = self._spawn()
        except OSError:
            self._spawn_error = f"无法启动 Pi：{self.executable}"
            logger.warning(self._spawn_error)
            return ()
        try:
            self._send(proc, {"id": "list", "type": "get_commands"})
            events = self._read_events(
                proc, timeout_s=self.list_timeout_s, stop_on_command="get_commands"
            )
        except Exception as exc:
            self._spawn_error = str(exc)
            logger.warning("Pi get_commands 失败：%s", exc)
            return ()
        finally:
            self._close(proc)
        if self._last_stop == "timeout":
            self._spawn_error = "列工具目录超时"
            logger.warning(self._spawn_error)
            return ()
        # 列目录成功：清掉残留的启动错误，避免上一轮失败挡住本轮。
        self._spawn_error = ""
        found: list[Mapping[str, Any]] = []
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
                info = cmd.get("sourceInfo")
                info = info if isinstance(info, Mapping) else {}
                source = str(
                    info.get("source") or cmd.get("source") or ""
                ).strip()
                path = str(
                    info.get("path") or cmd.get("path") or ""
                ).strip()
                entry: dict[str, Any] = {"name": name, "description": description}
                if source:
                    entry["source"] = source
                if path:
                    entry["path"] = path
                meta = self._skill_metadata(path)
                if meta:
                    entry["meta"] = meta
                    params_schema = meta.get("params_schema")
                    if isinstance(params_schema, Mapping):
                        entry["params"] = params_schema
                found.append(entry)
        return tuple(found)

    def execute(self, request: ToolRequest) -> tuple[ToolFeedback, ...]:
        return tuple(self.iter_execute(request))

    def iter_execute(
        self,
        request: ToolRequest,
        cancel: threading.Event | None = None,
    ) -> Iterator[ToolFeedback]:
        """边读边交出反馈。未映射事件丢掉。不 yield estimate。

        中间每一次 ``tool_execution_*`` 只记 **progress**；整轮 ``agent_settled``
        （或读循环结束）后才交 **一条** RESULT。否则读 SKILL.md / ls 目录也会被
        当成终态，包装就会诚实写成「只有脚本清单、没有天气」。
        """
        prompt = self._build_prompt(request)
        proc = self._spawn()
        active_calls: set[str] = set()
        completed_calls: set[str] = set()
        started = time.monotonic()
        usage: Mapping[str, Any] = {}
        self._last_stop = ""
        last_shell_ok = ""
        shell_ok_parts: list[str] = []
        last_tool_ok = ""
        last_tool_error = ""
        assistant_text = ""
        timeout_s = self.timeout_s
        raw_timeout = (
            request.meta.get("timeout_s") if isinstance(request.meta, Mapping) else None
        )
        if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool):
            if float(raw_timeout) > 0:
                timeout_s = float(raw_timeout)
        try:
            self._send(
                proc,
                {"id": request.request_id, "type": "prompt", "message": prompt},
            )
            for event in self._iter_events(
                proc,
                timeout_s=timeout_s,
                stop_on_settled=True,
                cancel=cancel,
            ):
                usage = _merge_usage(usage, _usage_from_event(event))
                elapsed_ms = int((time.monotonic() - started) * 1000)
                event_type = event.get("type")
                if event_type == "message_end":
                    text = self._assistant_text(event)
                    if text:
                        assistant_text = text
                if event_type == "tool_execution_start":
                    active_calls.add(_tool_call_key(event, len(active_calls)))
                elif event_type == "tool_execution_end":
                    completed_calls.add(_tool_call_key(event, len(active_calls)))
                    content, is_error = self._tool_end_content(event)
                    tool_name = str(event.get("toolName") or "").strip().lower()
                    if is_error:
                        if content:
                            last_tool_error = content
                    elif content:
                        last_tool_ok = content
                        if tool_name in _SHELL_TOOLS:
                            last_shell_ok = content
                            text = content.strip()
                            if text and text not in shell_ok_parts:
                                shell_ok_parts.append(text)
                converted = self._convert_event(
                    request,
                    event,
                    usage=usage,
                    elapsed_ms=elapsed_ms,
                )
                if converted is not None:
                    yield converted
        finally:
            self._close(proc)

        stop_reason = self._last_stop
        if stop_reason == "cancelled" or (cancel is not None and cancel.is_set()):
            yield ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(status=ToolStatus.ABORTED, error="已取消"),
            )
            return
        if stop_reason == "timeout":
            yield ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(status=ToolStatus.FAILED, error="工具执行超时"),
            )
            return

        elapsed_ms = int((time.monotonic() - started) * 1000)
        # 多段脚本输出一并保留（汇率 + 检索等），丢掉 ls / 读 SKILL 探路噪声。
        shell_joined = _join_shell_result_parts(shell_ok_parts)
        shell_fallback = (
            ""
            if _shell_output_is_exploration(last_shell_ok)
            else (last_shell_ok or "").strip()
        )
        # 探路过滤只用来决定「优先给哪一段」，不能把有输出变成没输出：
        # 只剩探路内容时也要如实交出去（由包装写成「只拿到了清单」），
        # 否则整轮会退化成 failed + 空摘要，下游连拿到了什么都不知道。
        tool_fallback = (last_tool_ok or "").strip()
        full = (
            shell_joined or shell_fallback or assistant_text or tool_fallback or ""
        ).strip()
        if len(full) > 4000:
            full = full[:4000].rstrip() + "…"
        # summary 给包装：多段均分，不能只截开头 200 字（否则汇率落在后面就丢了）。
        short = _compact_shell_summary(shell_ok_parts, limit=1500)
        if not short:
            short = full[:1500] if full else ""
        if full:
            yield ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(
                    status=ToolStatus.OK,
                    result={"content": full},
                    summary=short,
                    ideal=True,
                    cost=_usage_cost(usage),
                    time_ms=elapsed_ms,
                    resource=_usage_resource(usage),
                ),
            )
            return
        if active_calls and not active_calls.issubset(completed_calls):
            yield ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(status=ToolStatus.PARTIAL, error="部分工具未完成"),
            )
            return
        if last_tool_error:
            yield ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(
                    status=ToolStatus.FAILED,
                    summary=(last_tool_error or "").strip()[:200],
                    ideal=False,
                    ideal_note=f"工具报错：{last_tool_error[:120]}",
                    cost=_usage_cost(usage),
                    time_ms=elapsed_ms,
                    resource=_usage_resource(usage),
                    error=last_tool_error,
                ),
            )
            return
        yield ToolFeedback(
            request_id=request.request_id,
            kind=FeedbackKind.RESULT,
            result=ToolResult(status=ToolStatus.FAILED, error="Pi 会话未产出工具结果"),
        )

    def _build_prompt(self, request: ToolRequest) -> str:
        if request.create is not None:
            return self._build_create_prompt(request)
        agent_plan = request.meta.get("agent_plan")
        if isinstance(agent_plan, Mapping):
            return self._build_agent_loop_prompt(request, agent_plan)
        skill_prompt = self._skill_slash_command(request.command, request)
        if skill_prompt:
            return skill_prompt
        params = json.dumps(request.params, ensure_ascii=False) if request.params else "{}"
        command = (request.command or "").strip() or "（由你选用）"
        field_ref = (
            json.dumps(dict(request.field_ref), ensure_ascii=False)
            if request.field_ref
            else "（无）"
        )
        feedback_plan = request.meta.get("feedback_plan") or {}
        feedback_text = (
            json.dumps(dict(feedback_plan), ensure_ascii=False)
            if isinstance(feedback_plan, Mapping) and feedback_plan
            else "（无）"
        )
        return "\n".join(
            [
                "请完成下面这一次工具使用。",
                "",
                "【目标】",
                request.need or "（未指定）",
                "",
                "【工具】",
                command,
                "",
                "【参数】",
                params,
                "",
                "【期望结果】",
                request.expected_result or "（未指定）",
                "",
                "【现场引用】",
                field_ref,
                "",
                "【反馈要求】",
                feedback_text,
                "",
                "【约束】",
                "只做这次工具使用，不要扩展到无关任务。",
                "按反馈要求输出关键阶段；没有要求就只输出必要结果。",
            ]
        )

    @staticmethod
    def _skill_slash_command(command: str, request: ToolRequest) -> str:
        """技能按 Pi 官方形式调用：``/skill:<name> <args>``。

        官方文档（Skills → Skill Commands）写明：技能注册成 ``/skill:name`` 命令，
        「Load and execute the skill」，命令后面的参数会作为 ``User: <args>``
        追加到技能正文之后。

        2026-09-13 实测：把 ``skill:get-weather`` 写进「【工具】」栏时，Pi 只会
        用 read 去读 SKILL.md，然后把**文件内容**当结果交回来，从不执行脚本；
        换成 ``/skill:get-weather 杭州 明天`` 就一次跑通。名字没错，调用形式错了。

        参数按官方语义走**自由文本**（官方例子 ``/skill:pdf-tools extract``）：
        直接给这次的需求原话，不要自己发明 ``key=value`` 之类的结构化写法——
        技能正文里怎么写参数，交给模型照着填。
        """
        if not command.startswith("skill:"):
            return ""
        name = command[len("skill:") :].strip()
        if not name:
            return ""
        args = str(request.need or "").strip()
        line = f"/skill:{name}"
        if args:
            line += " " + args
        return line

    def _build_agent_loop_prompt(
        self, request: ToolRequest, plan: Mapping[str, Any]
    ) -> str:
        steps = plan.get("steps")
        if not isinstance(steps, Sequence):
            steps = ()
        step_lines = []
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                continue
            deps = step.get("depends_on") or []
            step_lines.append(
                f"{index + 1}. {step.get('step_id') or 'step'}"
                f" | 工具：{step.get('command') or '（由你选用）'}"
                f" | 参数：{json.dumps(dict(step.get('params') or {}), ensure_ascii=False)}"
                f" | 依赖：{json.dumps(list(deps), ensure_ascii=False)}"
            )
        return "\n".join(
            [
                "这是一个多步骤工具任务。你可以在一个会话里按顺序或并行执行多个工具。",
                "",
                "【总目标】",
                request.need or plan.get("need") or "（未指定）",
                "",
                "【建议步骤】",
                *step_lines,
                "",
                "【执行规则】",
                "1. 只使用当前会话可用的工具。",
                "2. 步骤之间有依赖时，先完成前置步骤，再执行后续步骤。",
                "3. 步骤之间没有依赖时，可以并行执行。",
                "4. 每一步都输出关键进展；最终给出总结果。",
                "5. 不要做与本目标无关的工作。",
            ]
        )

    def _build_create_prompt(self, request: ToolRequest) -> str:
        """造工具：不点名现成命令，让引擎新建一个可重复调用的工具。"""
        spec = request.create
        assert spec is not None
        skill_name = _skill_name(spec.tool_name)
        skill_dir = self._agent_dir / "skills" / skill_name
        skill_path = skill_dir / "SKILL.md"
        tool_json_path = skill_dir / "tool.json"
        schema = (
            json.dumps(dict(spec.params_schema), ensure_ascii=False)
            if spec.params_schema
            else "{}"
        )
        intent = (spec.tool_intent or request.need or "").strip()
        expected = (spec.expected_output or request.expected_result or "").strip()
        estimate = request.meta.get("estimate")
        tool_meta = {
            "tool_name": skill_name,
            "tool_intent": intent,
            "params_schema": dict(spec.params_schema),
            "expected_output": expected,
            "cost_estimate": spec.cost_estimate,
            "feedback_plan": dict(spec.feedback_plan),
            "estimate": dict(estimate) if isinstance(estimate, Mapping) else {},
        }
        tool_meta_json = json.dumps(tool_meta, ensure_ascii=False, indent=2)
        body_lines = [
            f"# {skill_name}",
            "",
            f"能力意图：{intent}",
            "",
            "## 参数",
            f"参数格式：{schema}",
            "",
        ]
        if expected:
            body_lines += ["## 预期产出", expected, ""]
        if spec.feedback_plan:
            feedback = json.dumps(
                dict(spec.feedback_plan), ensure_ascii=False, indent=2
            )
            body_lines += [
                "## 反馈方案",
                "按下面阶段输出关键进展，并把关键结果字段写清：",
                feedback,
                "",
            ]
        body_lines += [
            "## 用法",
            "根据参数完成上面的能力意图。若需要脚本，请把脚本放在同目录 scripts/ 下，并在本文说明如何调用。",
            "",
            f"这次要办的事：{request.need}",
        ]
        body = "\n".join(body_lines)
        return (
            "请创建一个可复用的 Pi skill，不要只是这次把事办了。\n\n"
            f"必须创建这个文件：{skill_path}\n"
            f"还必须创建这个规格文件：{tool_json_path}\n"
            "规格文件内容严格如下（JSON）：\n"
            f"{tool_meta_json}\n\n"
            "文件必须以 YAML frontmatter 开头，且包含：\n"
            f"name: {skill_name}\n"
            f"description: {intent}\n"
            "正文如下（可补充，但 frontmatter 的 name 不能改）：\n\n"
            f"{body}\n\n"
            "只创建这个 skill 和必要脚本，不要做无关探索；完成后说明文件路径。"
        )

    def _convert_event(
        self,
        request: ToolRequest,
        event: Mapping[str, Any],
        *,
        usage: Mapping[str, Any] | None = None,
        elapsed_ms: int | None = None,
    ) -> ToolFeedback | None:
        usage = usage or {}
        event_type = event.get("type")
        if event_type == "tool_execution_start":
            return ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.PROGRESS,
                progress=ToolProgress(
                    stage="start",
                    step=str(event.get("toolName") or ""),
                    partial="",
                    time_used_ms=elapsed_ms,
                    resource_used=_usage_resource(usage),
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
                    cost_used=_usage_cost(usage),
                    time_used_ms=elapsed_ms,
                    resource_used=_usage_resource(usage),
                    note="工具执行中",
                ),
            )
        if event_type == "tool_execution_end":
            # 中间工具结束只记进度；整轮 RESULT 由 iter_execute 在 settled 后统一交。
            result = event.get("result") or {}
            is_error = bool(event.get("isError"))
            content = self._content_text(result.get("content"))
            tool_name = str(event.get("toolName") or "")
            return ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.PROGRESS,
                progress=ToolProgress(
                    stage="tool_done",
                    step=tool_name,
                    partial=(content or "").strip()[:200],
                    cost_used=_usage_cost(usage),
                    time_used_ms=elapsed_ms,
                    resource_used=_usage_resource(usage),
                    note=(
                        f"工具结束 {tool_name}"
                        + ("（报错）" if is_error else "")
                    ),
                ),
            )
        return None

    def _tool_end_content(self, event: Mapping[str, Any]) -> tuple[str, bool]:
        result = event.get("result") or {}
        content = (self._content_text(result.get("content")) or "").strip()
        return content, bool(event.get("isError"))

    def _assistant_text(self, event: Mapping[str, Any]) -> str:
        message = event.get("message")
        if not isinstance(message, Mapping):
            return ""
        if str(message.get("role") or "") != "assistant":
            return ""
        return (self._content_text(message.get("content")) or "").strip()

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
