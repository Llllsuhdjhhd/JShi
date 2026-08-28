"""对话会话：口头与元数据分离；斜杠命令不建活动。"""

from __future__ import annotations

import pytest

from jshi.app.cli import _runtime
from jshi.app.talk_session import TalkSession, TalkSetupError, prepare_talk
from jshi.identity import IdentityProfile


@pytest.fixture(autouse=True)
def _echo_model(monkeypatch):
    monkeypatch.delenv("JSHI_MODEL_ENDPOINT", raising=False)
    monkeypatch.delenv("JSHI_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("JSHI_MODEL_NAME", raising=False)


class CountingProcess:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.experience_calls = 0

    def experience(self, *args, **kwargs):
        self.experience_calls += 1
        return self._inner.experience(*args, **kwargs)

    def preview_state(self, *args, **kwargs):
        return self._inner.preview_state(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _make_session(tmp_path, process=None) -> TalkSession:
    host, identities, _subjects = _runtime(tmp_path)
    identities.create(
        IdentityProfile(
            subject_id="stone",
            name="匠石",
            origin="测试",
            narrative="测试主体",
        )
    )
    inner = process if process is not None else host
    return TalkSession(
        inner,
        data_dir=tmp_path,
        subject_id="stone",
        speaker="dp",
        channel=None,
        carriers=(),
    )


def test_utterance_separates_speech_and_meta(tmp_path):
    session = _make_session(tmp_path)
    outcome = session.handle("你好")
    kinds = [event.kind for event in outcome.events]
    assert kinds == ["speech", "meta"]
    speech, meta = outcome.events
    assert "我听见了" in speech.text
    assert "活跃区 v" in meta.text
    assert "活跃区" not in speech.text
    assert not outcome.quit


def test_speaker_writes_session_without_experience(tmp_path):
    host, identities, _subjects = _runtime(tmp_path)
    identities.create(
        IdentityProfile(
            subject_id="stone", name="匠石", origin="测试", narrative="测试主体"
        )
    )
    counted = CountingProcess(host)
    session = TalkSession(
        counted,
        data_dir=tmp_path,
        subject_id="stone",
        speaker="dp",
        channel=None,
        carriers=(),
    )
    outcome = session.handle("/speaker 宝玉")
    assert counted.experience_calls == 0
    assert session.speaker == "宝玉"
    assert any("宝玉" in event.text for event in outcome.events)
    stored = (tmp_path / "cli_session.json").read_text(encoding="utf-8")
    assert "宝玉" in stored


def test_plan_and_context_do_not_create_activity(tmp_path):
    host, identities, _subjects = _runtime(tmp_path)
    identities.create(
        IdentityProfile(
            subject_id="stone", name="匠石", origin="测试", narrative="测试主体"
        )
    )
    counted = CountingProcess(host)
    session = TalkSession(
        counted,
        data_dir=tmp_path,
        subject_id="stone",
        speaker="dp",
        channel=None,
        carriers=(),
    )
    session.handle("第一句")
    assert counted.experience_calls == 1
    plan = session.handle("/plan")
    context = session.handle("/context")
    assert counted.experience_calls == 1
    assert plan.events[0].kind == "overlay"
    assert "mode=" in plan.events[0].text
    assert context.events[0].kind == "overlay"
    assert "装载：" in context.events[0].text


def test_busy_rejects_second_utterance(tmp_path):
    session = _make_session(tmp_path)
    assert session._turn_lock.acquire(blocking=False)
    try:
        outcome = session.handle("第二句")
        assert outcome.events[0].kind == "notice"
        assert "尚未结束" in outcome.events[0].text
    finally:
        session._turn_lock.release()


def test_unknown_slash_does_not_call_experience(tmp_path):
    host, identities, _subjects = _runtime(tmp_path)
    identities.create(
        IdentityProfile(
            subject_id="stone", name="匠石", origin="测试", narrative="测试主体"
        )
    )
    counted = CountingProcess(host)
    session = TalkSession(
        counted,
        data_dir=tmp_path,
        subject_id="stone",
        speaker="dp",
        channel=None,
        carriers=(),
    )
    outcome = session.handle("/foo")
    assert counted.experience_calls == 0
    assert outcome.events[0].kind == "notice"
    assert "未知命令" in outcome.events[0].text


def test_prepare_talk_requires_speaker(tmp_path):
    process, identities, _subjects = _runtime(tmp_path)
    identities.create(
        IdentityProfile(
            subject_id="stone", name="匠石", origin="测试", narrative="测试主体"
        )
    )
    with pytest.raises(TalkSetupError):
        prepare_talk(process, identities, tmp_path, "stone", None, None, ())
