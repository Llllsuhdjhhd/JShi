"""对话会话：口头与元数据分离；斜杠命令不建活动。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jshi.app.cli import _runtime
from jshi.app.talk_session import (
    TalkSession,
    TalkSetupError,
    format_turn_meta,
    prepare_talk,
    recall_status,
)
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


def test_format_turn_meta_splits_zone_and_ledger():
    text = format_turn_meta(
        mode="respond",
        speaker_label="lux",
        speaker_status="confirmed",
        embodied="",
        recall_count=0,
        recall_reason="召回空",
        zone_blocks=8,
        ledger_version=26,
        boot="否",
        delivery="skipped",
    )
    assert "片场 8 块" in text
    assert "木头 v26" in text
    assert "回忆 0（召回空）" in text
    assert "boot 否" in text
    assert "活跃区 v" not in text


def test_recall_status_reports_empty_and_error():
    empty = SimpleNamespace(
        fragments=(),
        source_report=(
            SimpleNamespace(source="memory", error=None, skipped_ids=()),
        ),
    )
    count, reason = recall_status(empty)
    assert count == 0
    assert reason == "召回空"
    broken = SimpleNamespace(
        fragments=(),
        source_report=(
            SimpleNamespace(source="memory", error="qdrant timeout", skipped_ids=()),
        ),
    )
    count, reason = recall_status(broken)
    assert count == 0
    assert reason.startswith("错误：")


def test_last_before_utterance_does_not_call_experience(tmp_path):
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
    outcome = session.handle("/last")
    assert counted.experience_calls == 0
    assert "先说一句" in outcome.events[0].text


def test_utterance_separates_speech_and_meta(tmp_path):
    session = _make_session(tmp_path)
    outcome = session.handle("你好")
    kinds = [event.kind for event in outcome.events]
    assert kinds == ["speech", "meta"]
    speech, meta = outcome.events
    assert "我听见了" in speech.text
    assert "木头 v" in meta.text
    assert "片场 " in meta.text
    assert "回忆 " in meta.text
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
    last = session.handle("/last")
    memory = session.handle("/memory")
    assert counted.experience_calls == 1
    assert plan.events[0].kind == "overlay"
    assert "mode=" in plan.events[0].text
    assert context.events[0].kind == "overlay"
    assert "装载：" in context.events[0].text
    assert "片场：" in context.events[0].text
    assert "木头账本：" in context.events[0].text
    assert last.events[0].kind == "overlay"
    assert "本轮输入：" in last.events[0].text
    assert "回忆 " in last.events[0].text
    assert memory.events[0].kind == "overlay"
    assert "记忆游标" in memory.events[0].text


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


def test_runtime_reloads_active_zone_after_new_process(tmp_path):
    first, identities, _subjects = _runtime(tmp_path)
    identities.create(
        IdentityProfile(
            subject_id="stone", name="匠石", origin="测试", narrative="测试主体"
        )
    )
    first.experience("stone", "你好，记下这句。", object_ref="dp")
    view = first.active_zone.load("stone")
    assert view.version >= 1
    assert view.context_text

    second, _identities, _again = _runtime(tmp_path)
    restored = second.active_zone.load("stone")
    assert restored.version == view.version
    assert restored.context_text == view.context_text


def test_timing_command_reads_last_round_without_experience(tmp_path):
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
    first = session.handle("你好")
    spoken = "\n".join(event.text for event in first.events)
    assert "合计" not in spoken
    assert "①落位" not in spoken
    assert counted.experience_calls == 1

    outcome = session.handle("/timing")
    assert counted.experience_calls == 1
    assert outcome.events[0].kind == "overlay"
    text = outcome.events[0].text
    assert "上一轮 合计" in text
    assert "①落位" in text
    assert "⑤认知" in text

    empty = _make_session(tmp_path / "other")
    notice = empty.handle("/timing")
    assert notice.events[0].kind == "notice"
    assert "还没有走完一轮" in notice.events[0].text

    session.handle("第二句")
    assert counted.experience_calls == 2
    two = session.handle("/timing 2")
    assert counted.experience_calls == 2
    assert "第1轮" in two.events[0].text
    assert "第2轮" in two.events[0].text
    usage = session.handle("/timing x")
    assert "用法" in usage.events[0].text


def test_wrap_display_text_breaks_long_line():
    from jshi.app.talk_plain import wrap_display_text

    wrapped = wrap_display_text("abcdefghij", 4)
    assert wrapped == "abcd\nefgh\nij"
    chinese = wrap_display_text("你好世界啊", 8)
    assert chinese == "你好世界\n啊"


def test_prompt_works_before_any_utterance(tmp_path):
    session = _make_session(tmp_path)
    outcome = session.handle("/prompt")
    text = outcome.events[0].text
    assert "system\n" in text
    assert "【本轮】" in text
    assert "组装提示词失败" not in text


def test_prompt_command_shows_system_and_user(tmp_path):
    session = _make_session(tmp_path)
    session.handle("你好")
    outcome = session.handle("/prompt")
    text = outcome.events[0].text
    assert "【本轮】" in text
    assert "dp：你好" in text or "dp：" in text
    assert "system\n" in text
    assert "【正例】" not in text

