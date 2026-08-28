"""简易对话循环：一行 input，与 Textual 无关。"""

from __future__ import annotations

import shutil

from jshi.app.talk_session import TalkSession


def wrap_display_text(text: str, width: int) -> str:
    """按终端列宽折行。不依赖 Textual。"""
    columns = max(8, int(width))
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        if not paragraph:
            lines.append("")
            continue
        for start in range(0, len(paragraph), columns):
            lines.append(paragraph[start : start + columns])
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
