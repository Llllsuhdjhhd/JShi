"""认知 HTTP 提示：system / user 分层。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

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
    assert "lux" not in system
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

    assert "seg-a[2026-08-29 20:15]（lux）：昨天见的" in user
    assert "memory:extra[2026-07-01 09:30]（lux）：以前说过岭南" in user


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

    when = datetime(2026, 9, 7, 12, 30)
    assert (
        format_memory_line("五年级可读西游记", label="lux", occurred_at=when)
        == "[2026-09-07 12:30]（lux）五年级可读西游记"
    )
    assert format_memory_line("无归属", occurred_at=when) == "[2026-09-07 12:30]无归属"
    assert format_memory_line("只有名", label="lux") == "（lux）只有名"
    assert format_memory_line("  ") == ""
