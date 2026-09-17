"""认知 HTTP 提示：system / user 分层。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from jshi.core import Provenance, SubjectState
from jshi.models import ModelRequest, ModelSpeaker, OpenAICompatibleModel, build_system, build_user
from jshi.skill import CognitionSkill
from jshi.models.base import EchoModel


def _state(**kwargs) -> SubjectState:
    values = {
        "subject_id": "stone",
        "identity_summary": "匠石；来源：基础型匠石",
        "current_stance": "从共同基础继续成长。",
        "salient_values": ("与朋友交，言而有信。", "己所不欲，勿施于人。"),
        "commitments": (),
        "provenance": Provenance(source="test"),
    }
    values.update(kwargs)
    return SubjectState(**values)


def _request(**kwargs) -> ModelRequest:
    req = ModelRequest(
        purpose="subject_activity",
        input_text="你好吗",
        subject_state=_state(),
        speaker=ModelSpeaker(
            object_id="OBJ-b82c987295df4f9faf5ba2ea61846dfc",
            label="lux",
            status="confirmed",
        ),
        context=(
            {
                "id": "identity:stone",
                "kind": "identity_summary",
                "content": "匠石；来源：基础型匠石",
                "status": "active",
                "source": "identity",
            },
            {
                "id": "context-v5",
                "kind": "context_view",
                "content": "你还记得我吗\n哈喽",
                "status": "active",
                "source": "activity",
            },
            {
                "id": "context-v5-refs",
                "kind": "active_zone_refs",
                "content": "",
                "status": "active",
                "source": "activity",
                "segments": [
                    {"id": "seg-a", "text": "你还记得我吗"},
                    {"id": "seg-b", "text": "哈喽"},
                ],
            },
            {
                "id": "memory:dup",
                "kind": "fact",
                "content": "哈喽",
                "status": "active",
                "source": "memory",
            },
            {
                "id": "memory:extra",
                "kind": "fact",
                "content": "以前说过岭南",
                "status": "active",
                "source": "memory",
            },
            {
                "id": "personal:val-01",
                "kind": "value",
                "content": "与朋友交，言而有信。",
                "status": "accepted",
                "source": "personal",
            },
        ),
    )
    extra = CognitionSkill(EchoModel()).system_extra(req)
    req = replace(req, system_extra=extra)
    if kwargs:
        req = replace(req, **kwargs)
        if "system_extra" not in kwargs and req.purpose == "subject_activity":
            req = replace(req, system_extra=CognitionSkill(EchoModel()).system_extra(req))
    return req


def test_build_system_is_stable_prefix():
    system = build_system(_request())
    # 模板示例里本来就有说话人名字（lux），拿名字当判据会误报；
    # 这里要守的是「本轮材料不进 system 前缀」，用本轮输入里的独有内容来验。
    assert "你好吗" not in system
    assert "OBJ-" not in system
    assert "【正例】" not in system
    assert "【反例】" not in system
    assert "context-v5" not in system
    assert '"kind": "context_view"' not in system
    assert "你是匠石，来源" not in system
    assert "当前立场" not in system
    assert "【价值】" in system
    assert "与朋友交，言而有信。" in system
    assert "personal:val-" not in system
    assert system.count("与朋友交，言而有信。") == 1


def test_build_user_has_one_zone_and_no_object_id():
    user = build_user(_request())
    assert "lux：你好吗" in user
    assert "seg-a：你还记得我吗" in user
    assert "seg-b：哈喽" in user
    assert "object_id=" not in user
    assert "你还记得我吗\n哈喽" not in user
    assert user.count("哈喽") == 1
    assert "memory:extra：以前说过岭南" in user
    assert "memory:dup" not in user


def test_build_user_includes_tool_feedback_as_is():
    req = replace(_request(), tool_input="明天北京有雨，下午转阴。")
    user = build_user(req)
    assert "【在途工具】" not in user
    assert "明天北京有雨，下午转阴。" in user


def test_build_user_includes_tool_related_block():
    req = replace(
        _request(),
        tool_input=(
            "【工具相关】\n读法：绑定当前对话对象的工具材料。\n\n"
            "# 近期使用完成的工具\n- 需求：查杭州天气\n  工具名称：skill:get-weather\n"
            "  状态：已完成\n  最终的结果：晴"
        ),
    )
    user = build_user(req)
    assert "【工具相关】" in user
    assert "查杭州天气" in user
    assert "【工具热状态】" not in user


def test_build_user_includes_tool_hot_state_legacy():
    req = replace(
        _request(),
        tool_hot_state="【工具热状态】\n- 已完成 · skill:get-weather · 查杭州天气 · 晴 · 3分钟前",
    )
    user = build_user(req)
    assert "【工具热状态】" in user
    assert "已完成" in user
    assert "查杭州天气" in user


def test_build_user_distinguishes_ambiguous_names_without_object_id():
    req = _request(
        speaker=ModelSpeaker(
            object_id="OBJ-A",
            label="lux",
            aliases=("小卢",),
            status="provisional",
            reason="ambiguous_names",
        )
    )
    user = build_user(req)
    assert "未消歧" in user
    assert "object_id=" not in user
    assert "别名：小卢" in user


def test_reflection_keeps_raw_user_text():
    req = ModelRequest(
        purpose="reflection",
        input_text="回顾今天",
        subject_state=_state(salient_values=()),
    )
    assert build_user(req) == "回顾今天"
    assert "【本轮】" not in build_user(req)
    assert "【价值】" not in build_system(req)


def test_build_system_includes_current_time_when_provided():
    req = _request(now=datetime(2026, 8, 30, 12, 34, 0))

    system = build_system(req)

    assert "【当前时间】2026-08-30 12:34（周日）" in system


def test_build_user_labels_segments_and_memories_with_time():
    req = _request(
        now=datetime(2026, 8, 30, 12, 34, 0),
        context=(
            {
                "id": "context-v5-refs",
                "kind": "active_zone_refs",
                "content": "",
                "status": "active",
                "source": "activity",
                "segments": [
                    {
                        "id": "seg-a",
                        "text": "昨天见的",
                        "label": "lux",
                        "occurred_at": "2026-08-29T20:15:00",
                    },
                ],
            },
            {
                "id": "memory:extra",
                "kind": "fact",
                "content": "以前说过岭南",
                "status": "active",
                "source": "memory",
                "label": "lux",
                "occurred_at": "2026-07-01T09:30:00",
            },
        ),
    )

    user = build_user(req)

    # 时间用相对标签（精确时间仍在数据里）；now=2026-08-30 12:34
    assert "seg-a[半天前]（lux）：昨天见的" in user
    assert "memory:extra[2个月前]（lux）：以前说过岭南" in user


class _FakeChatResponse:
    def __init__(self, payload: bytes | list[bytes]) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeChatResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        assert isinstance(self._payload, bytes)
        return self._payload

    def __iter__(self):
        assert isinstance(self._payload, list)
        return iter(self._payload)


def test_openai_compatible_disables_thinking(monkeypatch) -> None:
    monkeypatch.delenv("JSHI_MODEL_THINKING", raising=False)
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN201
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeChatResponse(
            json.dumps(
                {
                    "id": "chatcmpl-test",
                    "choices": [{"message": {"content": '{"mode":"respond"}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("jshi.models.base.urlopen", fake_urlopen)
    model = OpenAICompatibleModel(
        "https://api.deepseek.com/v1/chat/completions",
        "sk-test",
        "deepseek-v4-flash",
    )
    response = model.generate(_request())
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body
    assert "stream" not in body
    assert response.text == '{"mode":"respond"}'


def test_openai_compatible_thinking_can_be_enabled(monkeypatch) -> None:
    monkeypatch.setenv("JSHI_MODEL_THINKING", "enabled")
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN201
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeChatResponse(
            json.dumps(
                {"choices": [{"message": {"content": "{}"}}]}
            ).encode("utf-8")
        )

    monkeypatch.setattr("jshi.models.base.urlopen", fake_urlopen)
    OpenAICompatibleModel(
        "https://api.deepseek.com/v1/chat/completions",
        "sk-test",
        "deepseek-v4-flash",
    ).generate(_request())
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    ("env_value", "effort"),
    [
        ("low", "low"),
        ("high", "high"),
        ("max", "max"),
        ("medium", "high"),
    ],
)
def test_openai_compatible_thinking_effort_levels(
    monkeypatch, env_value: str, effort: str
) -> None:
    monkeypatch.setenv("JSHI_MODEL_THINKING", env_value)
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN201
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeChatResponse(
            json.dumps(
                {"choices": [{"message": {"content": "{}"}}]}
            ).encode("utf-8")
        )

    monkeypatch.setattr("jshi.models.base.urlopen", fake_urlopen)
    OpenAICompatibleModel(
        "https://api.deepseek.com/v1/chat/completions",
        "sk-test",
        "deepseek-v4-flash",
    ).generate(_request())
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == effort


def test_openai_compatible_thinking_rejects_unknown(monkeypatch) -> None:
    monkeypatch.setenv("JSHI_MODEL_THINKING", "maybe")
    with pytest.raises(ValueError, match="JSHI_MODEL_THINKING"):
        OpenAICompatibleModel(
            "https://api.deepseek.com/v1/chat/completions",
            "sk-test",
            "deepseek-v4-flash",
        ).generate(_request())


def test_openai_compatible_stream_also_disables_thinking(monkeypatch) -> None:
    monkeypatch.delenv("JSHI_MODEL_THINKING", raising=False)
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN201
        captured["body"] = json.loads(request.data.decode("utf-8"))
        event = json.dumps(
            {"choices": [{"delta": {"content": "你好"}}]}
        )
        return _FakeChatResponse(
            [f"data: {event}\n".encode("utf-8"), b"data: [DONE]\n"]
        )

    monkeypatch.setattr("jshi.models.base.urlopen", fake_urlopen)
    model = OpenAICompatibleModel(
        "https://api.deepseek.com/v1/chat/completions",
        "sk-test",
        "deepseek-v4-flash",
    )
    chunks = list(model.generate_stream(_request()))
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body
    assert body["stream"] is True
    assert chunks == ["你好"]


def test_format_memory_line_includes_time_and_label() -> None:
    from jshi.models import format_memory_line

    when = datetime.now(timezone.utc) - timedelta(days=5, hours=1)
    line = format_memory_line("五年级可读西游记", label="lux", occurred_at=when)
    # 时间改成相对标签（精确时间仍存在 occurred_at 里，不在这里显示）
    assert line.startswith("[")
    assert "]（lux）五年级可读西游记" in line
    assert "天前" in line or "周前" in line
    plain = format_memory_line("无归属", occurred_at=when)
    assert "]无归属" in plain and plain.startswith("[")
    assert format_memory_line("只有名", label="lux") == "（lux）只有名"
    assert format_memory_line("  ") == ""


def test_relative_time_label_tiers() -> None:
    from jshi.models.prompt import relative_time_label

    now = datetime(2026, 9, 12, 12, 0)
    cases = (
        (timedelta(seconds=30), "1分钟前"),
        (timedelta(minutes=5), "5分钟前"),
        (timedelta(minutes=40), "40分钟前"),
        (timedelta(minutes=59), "59分钟前"),
        (timedelta(minutes=90), "1小时前"),
        (timedelta(hours=5), "3小时前"),
        (timedelta(hours=20), "半天前"),
        (timedelta(hours=30), "1天前"),
        (timedelta(hours=60), "2天前"),
        (timedelta(days=5), "5天前"),
        (timedelta(days=10), "1周前"),
        (timedelta(days=18), "2周前"),
        (timedelta(days=25), "3周前"),
        (timedelta(days=45), "1个月前"),
        (timedelta(days=100), "2个月前"),
        (timedelta(days=200), "半年前"),
        (timedelta(days=300), "1年前"),
        (timedelta(days=800), "2年前"),
    )
    for delta, want in cases:
        assert relative_time_label(now - delta, now=now) == want, (delta, want)
    # 边界：60 分钟进到小时档
    assert relative_time_label(now - timedelta(minutes=60), now=now) == "1小时前"
    # 72 小时进到天档
    assert relative_time_label(now - timedelta(hours=73), now=now) == "3天前"
    # 未来时间退回绝对标签，不出现负数
    future = relative_time_label(now + timedelta(hours=3), now=now)
    assert "前" not in future
    assert relative_time_label(None) == ""
    assert relative_time_label("not-a-time") == ""


def test_relative_time_label_naive_is_local_not_utc() -> None:
    from jshi.models.prompt import relative_time_label

    # 东八区 11:26；无时区的 09:49 按本地算是 1 小时前，不能当成 UTC 推成「17:49」。
    now = datetime(2026, 9, 12, 11, 26, tzinfo=timezone(timedelta(hours=8)))
    naive = datetime(2026, 9, 12, 9, 49)
    assert relative_time_label(naive, now=now) == "1小时前"
    utc_same_instant = datetime(2026, 9, 12, 3, 7, tzinfo=timezone.utc)
    assert relative_time_label(utc_same_instant, now=now) == "19分钟前"
