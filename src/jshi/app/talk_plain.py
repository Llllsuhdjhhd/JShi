"""简易对话循环：一行 input，与 Textual 无关。"""

from __future__ import annotations

from jshi.app.talk_session import TalkSession


def run_plain(session: TalkSession) -> None:
    print("直接打字后回车即发送。命令见 /help")
    print(f"主体 {session.subject_id}；对象 {session.speaker}（长期）")
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
                print(f"匠石：{event.text}")
            else:
                print(event.text)
        if outcome.quit:
            return
