"""01 身份识别：对象档案、必填校验、门禁、落库时机与认知确认的测试。

对应 doc/design/01-身份识别.md 与 方案(coding)/01-身份识别.md。
"""

from __future__ import annotations

import pytest

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.experienceledger import OutputKind
from jshi.models import ModelRequest, ModelResponse, ObjectAssessment
from jshi.recognition import (
    CarrierEntry,
    ObjectProfile,
    ObjectProfileRepository,
    ProfileObjectRecognition,
)
from jshi.subject import HistoryKind, HistoryRecord, SubjectProcess, SubjectRepository


class FixedModel:
    name = "fixed-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="回应", model=self.name)


def runtime(tmp_path, model=None):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile("stone", "匠石", "测试基础型", "我是匠石。")
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(repository, identities, model or FixedModel())
    return process, repository


def test_repository_roundtrip(tmp_path):
    repo = ObjectProfileRepository(tmp_path / "subject.sqlite3")
    repo.create(
        ObjectProfile(
            object_id="OBJ-A",
            label="张三",
            aliases=("阿三",),
            carriers=(CarrierEntry(kind="voiceprint", value="vp-1"),),
            source="test",
        )
    )

    assert repo.get("OBJ-A").label == "张三"
    assert [item.object_id for item in repo.find_by_names("阿三")] == ["OBJ-A"]
    assert repo.find_by_names("李四") == ()
    assert repo.find_by_carrier("voiceprint", "vp-1").object_id == "OBJ-A"
    assert repo.find_by_carrier("face", "f-1") is None
    updated = repo.update_status("OBJ-A", "confirmed")
    assert updated.status == "confirmed"
    assert repo.get("OBJ-A").status == "confirmed"


def test_experience_requires_object_ref(tmp_path):
    process, repository = runtime(tmp_path)

    with pytest.raises(ValueError, match="object reference"):
        process.experience("stone", "你好")

    assert repository.list_history("stone") == ()


def test_known_object_matches_profile(tmp_path):
    process, repository = runtime(tmp_path)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="user", source="test", status="confirmed"
        )
    )

    result = process.experience("stone", "你好", object_ref="user")

    fact = repository.list_history("stone", HistoryKind.FACT)[0]
    assert fact.content["object_id"] == "OBJ-USER"
    assert fact.content["object_status"] == "confirmed"
    assert fact.content["object_ref"] == "user"
    assert result.speaker.object_id == "OBJ-USER"
    assert result.speaker.confidence == 0.85
    assert result.speaker.aliases == ()
    # 已存在对象不重复创建
    assert len(process.profiles.list()) == 1


def test_new_name_creates_provisional_profile_on_landing(tmp_path):
    process, repository = runtime(tmp_path)

    result = process.experience("stone", "你好", object_ref="李四")

    profiles = process.profiles.list()
    assert len(profiles) == 1
    assert profiles[0].label == "李四"
    assert profiles[0].status == "provisional"
    fact = repository.list_history("stone", HistoryKind.FACT)[0]
    assert profiles[0].source == fact.id
    assert result.speaker.confidence == 0.60
    assert result.speaker.aliases == ()
    event_types = [
        item.event_type
        for item in repository.list_history("stone", HistoryKind.SUBJECT)
    ]
    assert "object_resolved" in event_types


def test_channel_without_profile_creates_provisional(tmp_path):
    process, repository = runtime(tmp_path)

    result = process.experience("stone", "你好", channel="dev-7")

    assert result.speaker.status == "provisional"
    assert result.speaker.confidence == 0.95
    assert result.speaker.aliases == ()
    profile = process.profiles.list()[0]
    assert profile.status == "provisional"
    fact = repository.list_history("stone", HistoryKind.FACT)[0]
    assert fact.content["object_id"] == profile.object_id
    assert fact.content["channel"] == "dev-7"


def test_channel_match_is_strong_signal(tmp_path):
    process, _repository = runtime(tmp_path)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-DEV",
            label="设备好友",
            channel="dev-1",
            source="test",
            status="confirmed",
        )
    )

    result = process.experience(
        "stone", "你好", object_ref="设备好友", channel="dev-1"
    )

    assert result.speaker.object_id == "OBJ-DEV"
    assert result.speaker.confidence == 1.00


def test_carrier_match_gives_object_name(tmp_path):
    process, _repository = runtime(tmp_path)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-VP",
            label="声纹好友",
            carriers=(CarrierEntry(kind="voiceprint", value="vp-9"),),
            source="test",
            status="confirmed",
        )
    )

    result = process.experience(
        "stone",
        "你好",
        object_ref="随便",
        carriers=(CarrierEntry(kind="voiceprint", value="vp-9"),),
    )

    assert result.speaker.object_id == "OBJ-VP"
    assert result.speaker.label == "声纹好友"
    assert result.speaker.confidence == 0.98
    assert result.speaker.reason == "carrier_match"
    assert result.speaker.aliases == ()


def test_carrier_unmatched_creates_provisional_with_carrier(tmp_path):
    process, repository = runtime(tmp_path)

    result = process.experience(
        "stone",
        "你好",
        carriers=(CarrierEntry(kind="voiceprint", value="vp-new"),),
    )

    profile = process.profiles.list()[0]
    assert profile.status == "provisional"
    assert profile.carriers == (CarrierEntry(kind="voiceprint", value="vp-new"),)
    assert result.speaker.confidence == 0.85
    fact = repository.list_history("stone", HistoryKind.FACT)[0]
    assert fact.content["object_id"] == profile.object_id


def _register_duplicates(process):
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-Z1", label="张三", source="test", status="confirmed"
        )
    )
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-Z2", label="张三", source="test", status="confirmed"
        )
    )


def test_duplicate_names_resolved_by_memory(tmp_path):
    process, repository = runtime(tmp_path)
    _register_duplicates(process)
    # 给 OBJ-Z2 预置一条带对象标识的相关事实
    process.repository.add_history(
        HistoryRecord(
            subject_id="stone",
            kind=HistoryKind.FACT,
            event_type="external_input",
            content={"text": "张三喜欢围棋", "object_id": "OBJ-Z2"},
        )
    )

    result = process.experience("stone", "你说围棋怎么样", object_ref="张三")

    assert result.speaker.object_id == "OBJ-Z2"
    assert result.speaker.confidence == 0.60
    assert result.speaker.reason.startswith("memory_match")


def test_duplicate_names_without_memory_are_blocked(tmp_path):
    process, repository = runtime(tmp_path)
    _register_duplicates(process)

    with pytest.raises(ValueError, match="confidence too low"):
        process.experience("stone", "你好", object_ref="张三")

    assert repository.list_history("stone", HistoryKind.FACT) == ()
    event_types = [
        item.event_type
        for item in repository.list_history("stone", HistoryKind.SUBJECT)
    ]
    assert event_types == ["object_rejected"]
    assert len(process.profiles.list()) == 2  # 阻断不落库


class ConfirmModel:
    name = "confirm-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="确认对象",
            model=self.name,
            object_assessment=ObjectAssessment(
                conclusion="confirm", label="李四", reason="渠道与自称一致"
            ),
        )


def test_cognitive_confirmation_upgrades_profile(tmp_path):
    process, repository = runtime(tmp_path, model=ConfirmModel())

    result = process.experience("stone", "你好", object_ref="李四")

    assert process.profiles.list()[0].status == "confirmed"
    assert result.speaker.status == "confirmed"
    assessed = [
        item
        for item in repository.list_history("stone", HistoryKind.SUBJECT)
        if item.event_type == "object_identity_assessed"
    ]
    assert len(assessed) == 1
    assert assessed[0].content["conclusion"] == "confirm"


class DenyModel:
    name = "deny-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="否认对象",
            model=self.name,
            object_assessment=ObjectAssessment(conclusion="deny", reason="身份不符"),
        )


def test_deny_marks_rejected_and_blocks_next_activity(tmp_path):
    process, repository = runtime(tmp_path, model=DenyModel())
    process.experience("stone", "你好", object_ref="李四")
    assert process.profiles.list()[0].status == "rejected"

    with pytest.raises(ValueError, match="confidence too low"):
        process.experience("stone", "又见面了", object_ref="李四")

    event_types = [
        item.event_type
        for item in repository.list_history("stone", HistoryKind.SUBJECT)
    ]
    assert event_types.count("object_rejected") == 1


def test_confidence_is_dynamic_with_profile_status(tmp_path):
    process, _repository = runtime(tmp_path)

    first = process.experience("stone", "你好", object_ref="李四")
    assert first.speaker.confidence == 0.60  # 无档案：新建候选

    second = process.experience("stone", "继续", object_ref="李四")
    assert second.speaker.confidence == 0.65  # 暂定档案

    process.profiles.update_status(first.speaker.object_id, "confirmed")
    third = process.experience("stone", "再聊", object_ref="李四")
    assert third.speaker.confidence == 0.85  # 已确认档案


def test_resolver_rules(tmp_path):
    repo = ObjectProfileRepository(tmp_path / "subject.sqlite3")
    resolver = ProfileObjectRecognition(repo)

    new = resolver.resolve("stone", "你好", "李四")
    assert new.status == "provisional"
    assert new.confidence == 0.60
    assert new.object_id is not None

    repo.create(
        ObjectProfile(
            object_id=new.object_id, label="李四", source="x", status="provisional"
        )
    )
    hit = resolver.resolve("stone", "你好", "李四")
    assert hit.confidence == 0.65

    repo.update_status(new.object_id, "confirmed")
    hit2 = resolver.resolve("stone", "你好", "李四")
    assert hit2.confidence == 0.85

    with pytest.raises(ValueError, match="object reference"):
        resolver.resolve("stone", "你好", None)
    channel_new = resolver.resolve("stone", "你好", None, channel="dev-x")
    assert channel_new.status == "provisional"
    assert channel_new.confidence == 0.95
    assert channel_new.object_id


def test_object_id_ref_is_channel_bound(tmp_path):
    process, _repository = runtime(tmp_path)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER",
            label="user",
            aliases=("朋友",),
            source="test",
            status="confirmed",
        )
    )

    result = process.experience("stone", "你好", object_ref="OBJ-USER")

    assert result.speaker.object_id == "OBJ-USER"
    assert result.speaker.confidence == 1.00
    assert result.speaker.reason == "object_id_match"
    assert result.speaker.aliases == ("朋友",)


def test_name_match_carries_aliases(tmp_path):
    process, _repository = runtime(tmp_path)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-A",
            label="张三",
            aliases=("阿三",),
            source="test",
            status="confirmed",
        )
    )

    result = process.experience("stone", "你好", object_ref="阿三")

    assert result.speaker.object_id == "OBJ-A"
    assert result.speaker.label == "张三"
    assert result.speaker.aliases == ("阿三",)
    assert result.speaker.confidence == 0.85
    assert result.speaker.reason == "name_match"


def test_duplicate_names_close_scores_are_blocked(tmp_path):
    process, repository = runtime(tmp_path)
    _register_duplicates(process)
    process.repository.add_history(
        HistoryRecord(
            subject_id="stone",
            kind=HistoryKind.FACT,
            event_type="external_input",
            content={"text": "你好围棋", "object_id": "OBJ-Z1"},
        )
    )
    process.repository.add_history(
        HistoryRecord(
            subject_id="stone",
            kind=HistoryKind.FACT,
            event_type="external_input",
            content={"text": "你好围棋", "object_id": "OBJ-Z2"},
        )
    )

    with pytest.raises(ValueError, match="confidence too low"):
        process.experience("stone", "你好围棋", object_ref="张三")

    facts = repository.list_history("stone", HistoryKind.FACT)
    assert all(item.content.get("object_id") in {"OBJ-Z1", "OBJ-Z2"} for item in facts)
    assert len(process.profiles.list()) == 2


def test_normalize_text_replaces_speaker_and_wo():
    from jshi.recognition import normalize_text

    out = normalize_text(
        "小明：今天很累，我决定休息。我们明天见。",
        speaker_id="OBJ-A",
        subject_id="stone",
        speaker_names=("小明", "阿明"),
    )
    assert out == "OBJ-A：今天很累，OBJ-A决定休息。我们明天见。"


def test_normalize_text_subject_mentioned_and_unresolved():
    from jshi.recognition import normalize_text

    out = normalize_text(
        "我觉得小王的建议不错，李四也这么说。",
        speaker_id="stone",
        subject_id="stone",
        mentioned={"小王": "OBJ-W"},
    )
    assert out == "stone觉得OBJ-W的建议不错，李四也这么说。"


def test_experience_normalizes_text_after_landing(tmp_path):
    process, repository = runtime(tmp_path)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-A",
            label="小明",
            aliases=("阿明",),
            source="test",
            status="confirmed",
        )
    )
    process.experience(
        "stone", "阿明：今天很累，我决定休息。", object_ref="小明"
    )
    facts = repository.list_history("stone", HistoryKind.FACT)
    landing = next(item for item in facts if item.event_type == "external_input")
    assert landing.content["text"] == "阿明：今天很累，我决定休息。"
    assert landing.content["normalized_text"] == (
        "OBJ-A：今天很累，OBJ-A决定休息。"
    )
    segment = next(
        item
        for item in process.activity_ledger.list_experiences("stone")
        if item.output_kind == OutputKind.EXTERNAL_INPUT
    )
    assert segment.text_raw == "阿明：今天很累，我决定休息。"
    assert segment.text_normalized == "OBJ-A：今天很累，OBJ-A决定休息。"

