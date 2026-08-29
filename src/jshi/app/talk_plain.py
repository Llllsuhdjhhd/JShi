"""简易对话循环：一行 input，与 Textual 无关。"""

from __future__ import annotations

import shutil
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


def run_plain(session: TalkSession) -> None:
    print("直接打字后回车即发送。命令见 /help")
    print(f"主体 {session.subject_id}；对象 {session.speaker}（长期）")
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    while True:
        try:
            line = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        outcome = session.handle(line)
        for event in outcome.events:
            if event.kind == "speech":
                print(wrap_display_text(f"匠石：{event.text}", width))
            else:
                print(wrap_display_text(event.text, width))
        if outcome.quit:
            return
