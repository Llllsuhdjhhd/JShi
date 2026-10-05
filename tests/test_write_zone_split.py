"""05 回复与活跃区解耦：写场调用（write_zone）与回复调用独立。

- 注入 write_zone 时走两次调用：回复(cognition) 出 response_plan，写场(write_zone) 出
  rewritten_context / edit。
- 写场失败重试一次；仍失败 → 空写场结果（沿用上一份），不吞回复。
- 木头整份重写；人格走 edit + 程序追加（隐藏木头写场）。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

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
        self.last_request = None

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.last_request = request
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


def test_write_timing_records_retry_and_failure_separately(tmp_path):
    process, _ = runtime(tmp_path, ReplyModel(), FailingWriteModel(fail_times=2))
    result = process.experience("stone", "你好", object_ref="user")
    rows = [json.loads(line) for line in (tmp_path / "write_timings.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(row["activity_id"] == result.activity.id for row in rows)
    models = [row for row in rows if row["phase"] == "model" and row["status"] != "started"]
    assert [(row["attempt"], row["status"]) for row in models] == [(1, "error"), (2, "error")]
    total = next(row for row in rows if row["phase"] == "write" and row["status"] != "started")
    assert total["status"] == "failed"
    assert total["total_ms"] >= sum(row["total_ms"] for row in models)
    assert total["started_at"] <= total["finished_at"]


def test_deferred_write_timing_starts_only_when_write_runs(tmp_path):
    entered, release = Event(), Event()

    class BlockingWrite(WriteModel):
        def generate(self, request):
            entered.set()
            assert release.wait(5)
            return super().generate(request)

    process, _ = runtime(tmp_path, ReplyModel(), BlockingWrite())
    result = process.experience("stone", "你好", object_ref="user", defer_write=True)
    path = tmp_path / "write_timings.jsonl"
    assert not path.exists()
    job = process.take_deferred_write()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(job)
        try:
            assert entered.wait(5)
            started = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            assert [(row["phase"], row["status"]) for row in started] == [("write", "started"), ("model", "started")]
        finally:
            release.set()
        assert future.result(timeout=5)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert all(row["activity_id"] == result.activity.id for row in rows)
    assert rows[-1]["phase"] == "write"
    assert rows[-1]["status"] == "succeeded"
    assert rows[-1]["total_ms"] >= rows[-2]["total_ms"]


def test_persona_write_receives_current_response_in_actual_prompt(tmp_path):
    from jshi.models.prompt import build_user
    from jshi.skill import SkillModelPort, WriteZoneSkill
    from jshi.style import SMITH

    class DetailedReply(ReplyModel):
        def generate(self, request):
            return ModelResponse(model=self.name, response_plan=ResponsePlan(
                mode="respond",
                items=(ResponseItem("verbal", "这次先告诉你天气。", ("OBJ-USER",)),
                       ResponseItem("embodied", "点头")),
                unsaid="user 的路线结果暂缓告知。",
            ))

    write = WriteModel()
    process, _ = runtime(tmp_path, DetailedReply(), SkillModelPort(
        WriteZoneSkill(write), apply_to=("write_zone",)))
    process.style_packs.set("stone", SMITH)
    process.zone_store.boot("stone", value="我是匠石。", scene=["user 刚问过天气。"])
    process.experience("stone", "现在天气怎么样", object_ref="user")

    prompt = build_user(write.last_request)
    assert "【此时的输入】" in prompt
    assert "【此时的片场】" in prompt
    assert "【你的回应】" in prompt
    assert "这次先告诉你天气。" in prompt
    assert "点头" in prompt
    assert "user 的路线结果暂缓告知。" in prompt
    assert '"target_ids": ["user"]' in prompt
    assert '"target_ids": ["OBJ-USER"]' not in prompt
