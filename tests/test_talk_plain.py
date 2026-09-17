"""简易对话循环：提示符不得抢在口头前面；回显行不当成下一句输入。"""

from __future__ import annotations

import threading
import time

from jshi.app.talk_plain import _classify_input, run_plain
from jshi.app.talk_session import IdleTrial, TalkEvent, TalkOutcome


def test_classify_skips_echoed_speech_and_meta():
    assert _classify_input("哈喽") == "哈喽"
    assert _classify_input("你：哈喽") == "哈喽"
    assert _classify_input("你：你还记得我吗") == "你还记得我吗"
    assert _classify_input("") == ""
    assert _classify_input("你：") == ""
    assert _classify_input("匠石：嗨，lux，我在。") == ""
    assert _classify_input("你：匠石：嗨，lux，我在。（动作：转向声音来处，微微点头）") == ""
    assert (
        _classify_input(
            "[respond；lux/confirmed；回忆 34；片场 50 块；木头 v26；boot 否；投递 skipped（数据不足）]"
        )
        == ""
    )
    assert _classify_input("你：写法改为 阿丘（已记住）") == ""
    assert _classify_input("你：对象改为 lux（已记住）") == ""


def test_plain_prints_next_prompt_only_after_reply(monkeypatch):
    order: list[str] = []
    started = threading.Event()
    release = threading.Event()

    class FakeSession:
        subject_id = "stone"
        speaker = "lux"
        busy = False
        idle_trial = IdleTrial(enabled=False)
        on_reply = None

        def handle(self, line: str) -> TalkOutcome:
            order.append(f"handle:{line}")
            if line == "哈喽":
                started.set()
                assert release.wait(timeout=2)
                return TalkOutcome((TalkEvent("speech", "嗨，lux，我在。"),))
            if line == "/quit":
                return TalkOutcome(quit=True)
            return TalkOutcome()

        def handle_idle(self) -> TalkOutcome:
            return TalkOutcome()

    lines = iter(["哈喽", "/quit"])

    def fake_input(prompt: str) -> str:
        order.append(f"prompt:{prompt}")
        return next(lines)

    monkeypatch.setattr("builtins.input", fake_input)
    worker = threading.Thread(target=run_plain, args=(FakeSession(),), daemon=True)
    worker.start()
    assert started.wait(timeout=2)
    time.sleep(0.15)
    assert order == ["prompt:你：", "handle:哈喽"]
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert order == [
        "prompt:你：",
        "handle:哈喽",
        "prompt:你：",
        "handle:/quit",
    ]
