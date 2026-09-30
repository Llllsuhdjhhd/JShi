"""05 上下文带上记忆游标之后、尚未写入 09 的原话。"""

from __future__ import annotations

from jshi.experienceledger import ConsumerKind
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.memorycontrol import InProcessMemoryControl
from jshi.models import ModelRequest, ModelResponse
from jshi.models.prompt import build_user
from jshi.recognition import ObjectProfile
from jshi.subject import SubjectProcess, SubjectRepository


class CaptureModel:
    name = "capture"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(text="好的。", model=self.name, rewritten_context="现场")


def _process(tmp_path, model):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, model)
    process.profiles.create(
        ObjectProfile(object_id="OBJ-USER", label="user", source="test", status="confirmed")
    )
    return process


def _cognition_requests(model):
    return [r for r in model.requests if r.input_text in {"第一句", "第二句", "第三句"}]


def test_unsaved_dialogue_carries_prior_turns_but_not_current(tmp_path):
    model = CaptureModel()
    process = _process(tmp_path, model)
    process.experience("stone", "第一句", object_ref="user")
    process.experience("stone", "第二句", object_ref="user")

    second = _cognition_requests(model)[-1]
    assert second.input_text == "第二句"
    assert "第一句" in second.unsaved_dialogue
    assert "好的。" in second.unsaved_dialogue
    assert "第二句" not in second.unsaved_dialogue
    text = build_user(second)
    assert "【尚未存入记忆的近期原话】" in text
    assert text.index("【尚未存入记忆的近期原话】") < text.index("【本轮】")


def test_unsaved_dialogue_drops_segments_after_memory_cursor_moves(tmp_path):
    model = CaptureModel()
    process = _process(tmp_path, model)
    process.experience("stone", "第一句", object_ref="user")
    ledger = process.activity_ledger
    ledger.advance_consumer_cursor(
        "stone", ConsumerKind.MEMORY, ledger.head_sequence("stone")
    )
    process.experience("stone", "第三句", object_ref="user")
    last = _cognition_requests(model)[-1]
    assert last.input_text == "第三句"
    assert last.unsaved_dialogue == ""
    assert "【尚未存入记忆的近期原话】" not in build_user(last)


def test_default_flush_threshold_is_half_of_unsaved_cap():
    from jshi.core.params import memory_flush_chars, unsaved_dialogue_chars
    from jshi.experienceledger import InProcessExperienceLedger

    control = InProcessMemoryControl(InProcessExperienceLedger(), memory=None)
    assert control._flush_max_chars == memory_flush_chars() == 3000
    assert unsaved_dialogue_chars() == 2 * memory_flush_chars()
