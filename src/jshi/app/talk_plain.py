"""简易对话循环：一行 input，与 Textual 无关。"""

from __future__ import annotations

import queue
import re
import shutil
import threading
from unicodedata import east_asian_width

from jshi.app.talk_session import TalkSession


def cell_width(char: str) -> int:
    """终端显示列宽。汉字等全角为 2，ASCII 为 1。"""
    if ord(char) < 32:
        return 0
    kind = east_asian_width(char)
    if kind in {"F", "W", "A"}:
        return 2
    return 1


def wrap_display_text(text: str, width: int) -> str:
    """按终端显示列宽折行（中文按两列）。不依赖 Textual。"""
    columns = int(width)
    if columns < 1:
        columns = 80
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        if not paragraph:
            lines.append("")
            continue
        current: list[str] = []
        used = 0
        for char in paragraph:
            wide = cell_width(char)
            if wide == 0:
                current.append(char)
                continue
            if current and used + wide > columns:
                lines.append("".join(current))
                current = [char]
                used = wide
                continue
            current.append(char)
            used += wide
        lines.append("".join(current))
    return "\n".join(lines)


_ECHO_PROMPTS = ("你：", "你:")
_META_ECHO = re.compile(r"^\[(?:respond|think|wait|ignore)；")
_PROGRAM_OUTPUT_PREFIXES = (
    "匠石：",
    "匠石:",
    "Loading ",
    "Input length ",
    "已登录",
    "已退出",
    "已写入",
    "写法改为",
    "对象改为",
    "当前对象",
    "需要超级权限",
    "密码错误",
    "未配置",
    "用法：",
    "未知命令",
    "错误：",
    "上一轮尚未结束",
    "—— 闲时",
)


def _classify_input(line: str) -> str:
    """把一行输入分类：返回空串表示跳过，否则返回应作为输入的内容。"""
    text = line.strip()
    while True:
        matched = False
        for prefix in _ECHO_PROMPTS:
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                matched = True
                break
        if not matched:
            break
    if not text or _META_ECHO.match(text):
        return ""
    if any(text.startswith(prefix) for prefix in _PROGRAM_OUTPUT_PREFIXES):
        return ""
    return text


def run_plain(session: TalkSession) -> None:
    print("直接打字后回车即发送。命令见 /help")
    print(f"主体 {session.subject_id}；对象 {session.speaker}（长期）")
    if session.idle_trial.can_fire():
        print(session.idle_trial.banner())
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    # I-001 流式:模型支持时,response_plan 完整即先打印;否则整轮结束后统一打印。
    session.on_reply = lambda text: print(
        wrap_display_text(f"匠石：{text}", width), flush=True
    )

    def _print_outcome(outcome) -> bool:
        for event in outcome.events:
            if event.kind == "speech":
                if not event.text.strip():
                    continue
                print(wrap_display_text(f"匠石：{event.text}", width), flush=True)
            else:
                print(wrap_display_text(event.text, width), flush=True)
        return outcome.quit

    incoming: queue.Queue[str | None] = queue.Queue()
    prompt_ready = threading.Event()
    prompt_ready.set()

    def _reader() -> None:
        while True:
            prompt_ready.wait()
            prompt_ready.clear()
            try:
                raw = input("你：")
            except (EOFError, KeyboardInterrupt):
                incoming.put(None)
                return
            incoming.put(raw)

    threading.Thread(target=_reader, daemon=True).start()
    while True:
        timeout = session.idle_trial.remaining()
        try:
            raw = incoming.get(timeout=timeout)
        except queue.Empty:
            if session.busy or not session.idle_trial.due():
                continue
            # 读线程正停在「你：」上；先换行，口头不要印进提示符同一行。
            print(flush=True)
            if _print_outcome(session.handle_idle()):
                return
            continue
        if raw is None:
            print()
            return
        reopen = True
        try:
            line = _classify_input(raw)
            if line and _print_outcome(session.handle(line)):
                reopen = False
                return
        finally:
            if reopen:
                prompt_ready.set()
