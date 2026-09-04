"""风格包、整份活跃区重写、召回触发流水。"""

from __future__ import annotations

from jshi.experienceledger import InProcessExperienceLedger
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse
from jshi.recognition import ObjectProfile
from jshi.skill import CognitionSkill
from jshi.style import (
    DISPLAY_NAMES,
    SMITH,
    SUXIPO,
    WOOD,
    StylePack,
    StylePackRegistry,
    StylePackStore,
    builtin_packs,
    instruction_for,
    is_first_style_turn,
)
from jshi.subject import SubjectProcess, SubjectRepository


class CaptureModel:
    name = "capture-model"

    def __init__(self) -> None:
        self.request = None

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.request = request
        return ModelResponse(
            text="收到",
            model=self.name,
            rewritten_context=f"现场：{request.input_text}",
        )


def runtime(tmp_path, model=None):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(
        repository,
        identities,
        model or CaptureModel(),
        style_packs=StylePackStore(tmp_path / "style_pack.json"),
        recall_traces=None,
    )
    process.profiles.create(
        ObjectProfile(object_id="OBJ-USER", label="user", source="test", status="confirmed")
    )
    process.recall_traces.path = tmp_path / "recall_traces.jsonl"
    return process, repository


def test_empty_rewrite_keeps_previous_zone():
    ledger = InProcessExperienceLedger()
    ledger.save_rewritten_context("stone", "上一份现场")
    kept = ledger.save_rewritten_context("stone", "   ")
    assert kept.context_text == "上一份现场"
    assert kept.version == 1


def test_rewrite_truncates_to_cap():
    ledger = InProcessExperienceLedger(active_window_chars=8)
    view = ledger.save_rewritten_context("stone", "abcdefghijklmn")
    assert view.context_text == "abcdefgh"
    assert view.version == 1


def test_experience_saves_rewritten_zone(tmp_path):
    process, _repository = runtime(tmp_path)
    process.experience("stone", "今天见面", object_ref="user")
    view = process.activity_ledger.current_context_view("stone")
    assert view.context_text == "现场：今天见面"
    assert view.style_pack_id == WOOD


def test_style_pack_names_stay_out_of_prompt(tmp_path):
    process, _repository = runtime(tmp_path)
    process.style_packs.set("stone", SUXIPO)
    extra = CognitionSkill(CaptureModel()).system_extra(
        ModelRequest(
            purpose="subject_activity",
            input_text="你好",
            subject_state=process.assemble_current_state(
                "stone", "你好"
            ).subject_state,
            style_instruction=instruction_for(SUXIPO, first=True),
        )
    )
    joined = extra
    for name in DISPLAY_NAMES.values():
        assert name not in joined
    assert WOOD not in extra
    assert SUXIPO not in extra
    assert SMITH not in extra


def test_recall_trace_records_input_and_hits(tmp_path):
    process, _repository = runtime(tmp_path)
    process.experience("stone", "还记得上次吗", object_ref="user")
    traces = process.recall_traces.list("stone")
    assert len(traces) == 1
    assert traces[0].query == "还记得上次吗"
    assert traces[0].object_id == "OBJ-USER"
    assert traces[0].input_segment_id
    assert traces[0].activity_id


def test_first_turn_is_empty_zone_or_pack_switch():
    assert is_first_style_turn("", "", WOOD) is True
    assert is_first_style_turn("现场", WOOD, WOOD) is False
    assert is_first_style_turn("现场", WOOD, SUXIPO) is True
    assert is_first_style_turn("现场", "", WOOD) is True


def test_register_pack_without_changing_pipeline():
    registry = StylePackRegistry(builtin_packs())
    registry.register(
        StylePack(
            pack_id="extra",
            display_name="额外",
            instruction_first="首次槽",
            instruction_continue="续写槽",
        )
    )
    assert instruction_for("extra", first=True, registry=registry) == "首次槽"
    assert instruction_for("extra", first=False, registry=registry) == "续写槽"
    assert instruction_for(WOOD, first=True, registry=registry) == ""


def test_empty_zone_selects_first_slot(tmp_path):
    model = CaptureModel()
    process, _repository = runtime(tmp_path, model)
    process.experience("stone", "第一句", object_ref="user")
    assert model.request is not None
    assert model.request.style_first is True


def test_same_style_next_turn_selects_continue(tmp_path):
    model = CaptureModel()
    process, _repository = runtime(tmp_path, model)
    process.experience("stone", "第一句", object_ref="user")
    process.experience("stone", "第二句", object_ref="user")
    assert model.request is not None
    assert model.request.style_first is False


def test_switching_pack_selects_first_again(tmp_path):
    model = CaptureModel()
    process, _repository = runtime(tmp_path, model)
    process.experience("stone", "第一句", object_ref="user")
    process.style_packs.set("stone", SUXIPO)
    process.experience("stone", "换写法", object_ref="user")
    assert model.request is not None
    assert model.request.style_first is True
    extra = CognitionSkill(CaptureModel()).system_extra(
        ModelRequest(
            purpose="subject_activity",
            input_text="换写法",
            subject_state=process.assemble_current_state(
                "stone", "换写法"
            ).subject_state,
            style_instruction="按叙事整理现场。",
        )
    )
    assert "按叙事整理现场。" in extra
    assert "苏西坡" not in extra
    assert SUXIPO not in extra
