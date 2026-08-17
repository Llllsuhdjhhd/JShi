"""外部活动完整参与图的逐阶段测试。

目标流程（当前七阶段）：

    阶段①  记录入口：对象解析 + fact/external_input
    阶段②  活跃区装载
    阶段③  状态组装
    阶段④  活动建立
    阶段⑤  认知（response_plan）+ 06 标记
    阶段⑥  行动：有 verbal 才落 language_action
    阶段⑦  收尾：活动完成；30 尝试投递

虚线部分（意图、结果反馈、治理、embodied）仍锁定为占位。
09 与个人世界装载质量不在本文件验收。
"""

from __future__ import annotations

import pytest

from jshi.app import cli
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.recognition import ObjectProfile
from jshi.subject import (
    ActivityKind,
    ActivityStatus,
    CognitiveKind,
    EpistemicStatus,
    EvidenceKind,
    HistoryKind,
    PersonalKind,
    PersonalStatus,
    SubjectProcess,
    SubjectRepository,
)


class RecordingModel:
    """记录每一次模型请求的确定性测试模型。"""

    name = "recording-model"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(text="这是模型的回应。", model=self.name)


@pytest.fixture
def runtime(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile(
            subject_id="stone",
            name="匠石",
            origin="测试基础型",
            narrative="我是匠石，从共同基础出发。",
        )
    )
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    model = RecordingModel()
    process = SubjectProcess(repository, identities, model)
    process.profiles.create(
        ObjectProfile(
            object_id="OBJ-USER", label="user", source="test", status="confirmed"
        )
    )
    return process, repository, model, identities


# 阶段1：记录入口 -----------------------------------------------------------


def test_phase1_input_fact_is_recorded_first(runtime):
    process, repository, _model, _identities = runtime

    process.experience("stone", "今天有些疲倦", object_ref="user")

    facts = repository.list_history("stone", HistoryKind.FACT)
    assert [item.event_type for item in facts] == [
        "external_input",
        "language_action",
    ]
    assert facts[0].kind is HistoryKind.FACT
    assert facts[0].content["text"] == "今天有些疲倦"
    assert facts[0].content["source"] == "user"
    assert facts[0].content["object_id"] == "OBJ-USER"
    assert facts[0].content["object_status"] == "confirmed"
    assert facts[0].content["object_ref"] == "user"
    assert facts[0].source_ids == ()


# 阶段2：当前状态组装 -------------------------------------------------------


def test_phase2_assembly_loads_all_personal_world_systems(runtime):
    process, repository, _model, _identities = runtime
    process.add_personal_item("stone", PersonalKind.VALUE, "优先坦率表达")
    process.add_personal_item("stone", PersonalKind.COMMITMENT, "下次继续询问近况")
    process.add_personal_item("stone", PersonalKind.RELATIONSHIP, "与朋友的信任在加深")
    process.add_personal_item("stone", PersonalKind.CAPABILITY, "能耐心倾听")
    process.add_personal_item("stone", PersonalKind.AESTHETIC, "喜欢朴素真诚的表达")
    process.add_personal_item(
        "stone", PersonalKind.SELF_UNDERSTANDING, "还在学习如何拒绝"
    )
    concern = process.propose_open_matter(
        "stone", "继续理解朋友的疲倦", source_ids=("seed",)
    )
    process.experience("stone", "先打个招呼", object_ref="user")  # 为召回预置一段事实历史

    result = process.experience("stone", "今天又见面了", object_ref="user")
    assembled = result.current_state
    state = assembled.subject_state

    # 身份系统
    assert state.subject_id == "stone"
    assert "匠石" in state.identity_summary
    assert "测试基础型" in state.identity_summary
    assert state.current_stance == "我是匠石，从共同基础出发。"
    # 个人世界系统
    assert "优先坦率表达" in state.salient_values
    assert "下次继续询问近况" in state.commitments
    assert "继续理解朋友的疲倦" in state.concerns
    # 活跃区事件（concern）由 active_zone 独立承载，个人世界不再重复装载
    assert len(assembled.personal_items) == 6
    # 未完成现实系统：只装载已有主体面，不新增
    assert concern.id in assembled.active_event_ids
    assert len(repository.list_personal_items("stone", PersonalKind.CONCERN)) == 1
    # 阶段③ 不做全量文本召回：初始工作集不携带 recalled 片段（追加召回在认知阶段）
    assert assembled.recalled == ()


def test_phase2_initial_load_does_not_text_recall(runtime):
    process, repository, _model, _identities = runtime
    result = process.experience("stone", "今天有些疲倦", object_ref="user")

    assembled = process.assemble_current_state("stone", "今天有些疲倦")

    # 文本召回只属于阶段⑤ 追加召回；初始组装不按文本召回
    assert assembled.recalled == ()
    assert result.activity.trigger not in {item.event_id for item in assembled.recalled}


def test_phase2_assembly_provenance_marks_source(runtime):
    process, _repository, _model, _identities = runtime

    assembled = process.assemble_current_state("stone", "今天有些疲倦")

    provenance = assembled.subject_state.provenance
    assert provenance.source == "assembled_current_state"
    assert provenance.method == "load_existing_only"
    assert assembled.subject_state.uncertainties == ()


# 阶段3：活动建立 -----------------------------------------------------------


def test_phase3_activity_is_external_mounted_and_completed(runtime):
    process, repository, _model, _identities = runtime
    concern = process.propose_open_matter(
        "stone", "继续理解朋友的疲倦", source_ids=("seed",)
    )

    result = process.experience("stone", "今天有些疲倦", object_ref="user")

    activity = result.activity
    assert activity.kind is ActivityKind.EXTERNAL
    assert activity.status is ActivityStatus.COMPLETED
    # 占位模型不聚焦：活动挂载空（模型聚焦回填见 test_active_zone）
    assert activity.active_concern_ids == ()
    # 意图系统：字段存在但当前未填充（虚线缺口，锁定现状）
    assert activity.intention_ids == ()
    # 触发源指向阶段1的输入事实
    input_fact = repository.list_history("stone", HistoryKind.FACT)[0]
    assert activity.trigger == input_fact.id

    stored = repository.get_activity(activity.id)
    assert stored.status is ActivityStatus.COMPLETED
    assert stored.active_concern_ids == activity.active_concern_ids


# 阶段4：认知活动 -----------------------------------------------------------


def test_phase4_perception_is_accepted_report(runtime):
    process, _repository, _model, _identities = runtime

    result = process.experience("stone", "今天有些疲倦", object_ref="user")

    perception = result.perception
    assert perception.kind is CognitiveKind.PERCEPTION
    assert perception.epistemic_status is EpistemicStatus.ACCEPTED
    assert perception.evidence_kind is EvidenceKind.REPORT
    assert perception.activity_id == result.activity.id
    assert perception.content == "对方表达：今天有些疲倦"
    assert len(perception.source_ids) == 1  # 感知以输入事实为来源


def test_phase4_thought_is_considering_inference(runtime):
    process, repository, _model, _identities = runtime

    result = process.experience("stone", "今天有些疲倦", object_ref="user")

    thought = result.thought
    assert thought.kind is CognitiveKind.INFERENCE
    assert thought.epistemic_status is EpistemicStatus.CONSIDERING
    assert thought.evidence_kind is EvidenceKind.COGNITIVE_REASONING
    assert thought.model == "recording-model"
    # 来源链：感知 → 推断
    assert result.perception.id in thought.source_ids
    stored = repository.get_cognitive_content(thought.id)
    assert stored.epistemic_status is EpistemicStatus.CONSIDERING


def test_phase4_model_request_receives_subject_state_and_context(runtime):
    process, _repository, model, _identities = runtime
    process.add_personal_item("stone", PersonalKind.VALUE, "优先坦率表达")
    process.add_personal_item("stone", PersonalKind.COMMITMENT, "下次继续询问近况")
    process.add_personal_item("stone", PersonalKind.AESTHETIC, "喜欢朴素真诚的表达")
    concern = process.propose_open_matter(
        "stone", "继续理解朋友的疲倦", source_ids=("seed",)
    )
    process.experience("stone", "先打个招呼", object_ref="user")  # 预置召回

    result = process.experience("stone", "今天有些疲倦", object_ref="user")
    request = model.requests[-1]

    assert request.purpose == "subject_activity"
    assert request.input_text == "今天有些疲倦"
    assert request.subject_state.subject_id == "stone"
    assert "优先坦率表达" in request.subject_state.salient_values
    assert concern.id in result.current_state.active_event_ids

    kinds = {item["kind"] for item in request.context}
    assert {"value", "commitment", "event", "aesthetic"} <= kinds
    assert not any(item["kind"] == "recalled_fact" for item in request.context)


# 阶段5：行动与结果 ---------------------------------------------------------


def test_phase5_language_action_recorded_in_fact_history(runtime):
    process, repository, model, _identities = runtime

    result = process.experience("stone", "今天有些疲倦", object_ref="user")

    facts = repository.list_history("stone", HistoryKind.FACT)
    action = facts[-1]
    assert action.event_type == "language_action"
    assert action.kind is HistoryKind.FACT
    assert action.content["text"] == "这是模型的回应。"
    assert action.content["model"] == model.name
    assert action.content["activity_id"] == result.activity.id
    assert result.action_text == "这是模型的回应。"


def test_phase5_external_result_feedback_is_not_yet_implemented(runtime):
    """锁定当前缺口：行动结果反馈尚未实现（图中虚线部分）。"""
    process, repository, _model, _identities = runtime

    process.experience("stone", "今天有些疲倦", object_ref="user")

    event_types = {
        item.event_type for item in repository.list_history("stone", HistoryKind.FACT)
    }
    assert "external_result" not in event_types


# 阶段6：收尾与沉淀 ---------------------------------------------------------


def test_phase6_subject_history_records_assembly_and_cognition(runtime):
    process, repository, _model, _identities = runtime

    result = process.experience("stone", "今天有些疲倦", object_ref="user")

    subject = repository.list_history("stone", HistoryKind.SUBJECT)
    types = [item.event_type for item in subject]
    for required in (
        "context_window_loaded",
        "current_state_assembled",
        "activity_created",
        "cognitive_content_appeared",
        "activity_response_state",
        "activity_completed",
    ):
        assert required in types
    cognition = next(
        item for item in subject if item.event_type == "cognitive_content_appeared"
    )
    assert cognition.content["cognitive_content_id"] == result.thought.id
    assert cognition.content["epistemic_status"] == "considering"


def test_phase6_epistemic_transition_is_audited(runtime):
    process, repository, _model, _identities = runtime
    result = process.experience("stone", "今天有些疲倦", object_ref="user")

    updated = process.transition_cognition(
        result.thought.id,
        EpistemicStatus.PROVISIONAL,
        "目前证据有限，先暂时接受",
    )

    assert updated.epistemic_status is EpistemicStatus.PROVISIONAL
    transitions = repository.list_transitions(result.thought.id)
    assert [(t.from_state, t.to_state, t.reason) for t in transitions] == [
        ("considering", "provisional", "目前证据有限，先暂时接受")
    ]
    assert repository.list_history("stone", HistoryKind.SUBJECT)[-1].event_type == (
        "epistemic_transition"
    )


def test_phase6_proposed_open_matter_persists_into_next_activity(runtime):
    process, repository, _model, _identities = runtime
    result = process.experience("stone", "他看起来很累", object_ref="user")
    concern = process.propose_open_matter(
        "stone", "继续关心他的疲倦", source_ids=(result.thought.id,)
    )

    later = process.experience("stone", "又见面了", object_ref="user")
    assert later.activity.active_concern_ids == ()
    assert concern.id in later.current_state.active_event_ids
    assert "继续关心他的疲倦" in later.current_state.subject_state.concerns

    # 显式关闭后不再进入后续活动
    process.close_personal_item(concern.id, PersonalStatus.RELEASED, "暂时放下")
    final = process.experience("stone", "改天再聊", object_ref="user")
    # 不再作为未完成现实进入主体状态（完成事件可作为背景保留）
    assert final.current_state.subject_state.concerns == ()


# 阶段0：应用入口 -----------------------------------------------------------


def test_phase0_cli_experience_triggers_full_flow(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("JSHI_MODEL_ENDPOINT", raising=False)
    monkeypatch.delenv("JSHI_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("JSHI_MODEL_NAME", raising=False)
    data_dir = tmp_path / "cli-data"

    monkeypatch.setattr(
        "sys.argv", ["jshi", "--data-dir", str(data_dir), "create", "stone"]
    )
    cli.main()
    assert "已创建：stone" in capsys.readouterr().out

    monkeypatch.setattr(
        "sys.argv",
        [
            "jshi",
            "--data-dir",
            str(data_dir),
            "experience",
            "stone",
            "你好",
            "--speaker",
            "user",
        ],
    )
    cli.main()
    out = capsys.readouterr().out
    assert "我听见了：你好" in out
    assert "[活动" in out

    identities = IdentityRepository(data_dir / "identities.json")
    subjects = SubjectRepository(data_dir / "subject.sqlite3")
    facts = subjects.list_history("stone", HistoryKind.FACT)
    assert [item.event_type for item in facts] == ["external_input", "language_action"]
    activities = subjects.list_activities("stone")
    assert len(activities) == 1
    assert activities[0].kind is ActivityKind.EXTERNAL
    assert activities[0].status is ActivityStatus.COMPLETED
