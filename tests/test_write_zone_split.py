"""05 回复与活跃区解耦：写场调用（write_zone）与回复调用独立。

- 注入 write_zone 时走两次调用：回复(cognition) 出 response_plan，写场(write_zone) 出
  rewritten_context / edit。
- 写场失败重试一次；仍失败 → 空写场结果（沿用上一份），不吞回复。
- 木头整份重写；人格走 edit + 程序追加（隐藏木头写场）。
"""

from __future__ import annotations

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import (
    ModelPort,
    ModelRequest,
    ModelResponse,
    ResponseItem,
    ResponsePlan,
)
from jshi.recognition import ObjectProfile
from jshi.subject import SubjectProcess, SubjectRepository


class ReplyModel(ModelPort):
    name = "reply-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            model=self.name,
            response_plan=ResponsePlan(
                mode="respond",
                reason="接住对方",
                items=(ResponseItem(channel="verbal", text="你好，我接着说。"),),
            ),
        )


class WriteModel(ModelPort):
    name = "write-model"

    def __init__(self) -> None:
        self.calls = 0
        self.reply_in_context = False

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if any(item.get("kind") == "subject_reply" for item in request.context):
            self.reply_in_context = True
        # 木头整份重写：由写场调用产出。
        return ModelResponse(rewritten_context=f"现场：{request.input_text}", model=self.name)


class FailingWriteModel(ModelPort):
    name = "failing-write"

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("write_zone down")
        return ModelResponse(rewritten_context="现场：恢复", model=self.name)


def runtime(tmp_path, cognition: ModelPort, write_zone: ModelPort | None):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, cognition, write_zone=write_zone)
    process.profiles.create(
        ObjectProfile(object_id="OBJ-USER", label="user", source="test", status="confirmed")
    )
    return process, repository


def test_write_zone_split_uses_two_calls_and_saves_zone(tmp_path):
    write = WriteModel()
    process, _repository = runtime(tmp_path, ReplyModel(), write)

    result = process.experience("stone", "你好", object_ref="user")

    # 回复只由 cognition 交出口头；写场独立产整份现场。
    assert result.response_plan.verbal_text() == "你好，我接着说。"
    assert write.calls == 1
    assert write.reply_in_context  # 木头整份重写能拿到本轮回复
    applied = process.activity_ledger.current_context_view("stone")
    assert "现场：你好" in applied.context_text


def test_write_zone_failure_retries_once_and_keeps_reply(tmp_path):
    # 写场前两次失败 → 首次 + 重试一次都失败，返回空写场；回复不受影响。
    write = FailingWriteModel(fail_times=2)
    process, _repository = runtime(tmp_path, ReplyModel(), write)

    result = process.experience("stone", "你好", object_ref="user")

    assert write.calls == 2  # 首次 + 重试一次
    assert result.response_plan.verbal_text() == "你好，我接着说。"
    # 空写场：木头沿用上一份；无上一份时为空现场，不报错。
    applied = process.activity_ledger.current_context_view("stone")
    assert "现场" not in applied.context_text


def test_write_zone_success_after_one_failure(tmp_path):
    # 首次失败，重试成功 → 采用重试结果。
    write = FailingWriteModel(fail_times=1)
    process, _repository = runtime(tmp_path, ReplyModel(), write)

    process.experience("stone", "你好", object_ref="user")

    assert write.calls == 2
    applied = process.activity_ledger.current_context_view("stone")
    assert "现场：恢复" in applied.context_text
