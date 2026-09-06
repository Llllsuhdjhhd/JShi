"""苏西坡（小说家人格）：提示词内容、块级片场、输出解析、魔法数。"""

from __future__ import annotations

import json

from jshi.core import Provenance, SubjectState
from jshi.core.params import SUXIPO_ZONE_CHARS, suxipo_zone_chars
from jshi.models import ModelRequest, ModelResponse, ModelSpeaker
from jshi.skill import CognitionSkill
from jshi.style import (
    SUXIPO,
    SUXIPO_BOOT_INSTRUCTION,
    SUXIPO_INSTRUCTION,
    SUXIPO_SCHEMA,
    ZoneStore,
    instruction_for,
    is_persona,
    schema_for,
    zone_chars_for,
)


def _request(**kwargs):
    state = SubjectState(
        subject_id="stone",
        identity_summary="我是匠石。",
        current_stance="",
        salient_values=(),
        commitments=(),
        provenance=Provenance(source="test"),
    )
    return ModelRequest(
        purpose="subject_activity",
        input_text="茶凉了，还坐得住吗？",
        subject_state=state,
        speaker=ModelSpeaker(object_id="OBJ-U", label="路光", status="confirmed"),
        **kwargs,
    )


class FakeModel:
    name = "fake"

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text=json.dumps(self.payload, ensure_ascii=False), model=self.name
        )


def test_suxipo_zone_chars_magic_number():
    assert SUXIPO_ZONE_CHARS == 2000
    assert suxipo_zone_chars() == 2000


def test_suxipo_pack_content_and_contract():
    assert is_persona(SUXIPO) is True
    instruction = instruction_for(SUXIPO)
    assert instruction == SUXIPO_INSTRUCTION
    assert "【关于你】" in instruction
    assert "【你的任务】" in instruction
    assert "【价值】" in instruction
    assert "【文风】" in instruction
    assert "海明威" in instruction
    assert "不要填 id" in instruction
    assert '"edit": []' in instruction
    assert "本轮对方的话和你的回应由程序追加" in instruction
    # 配置名不进提示词
    assert "苏西坡" not in instruction
    assert "suxipo" not in instruction
    schema = schema_for(SUXIPO)
    assert schema is not None
    assert schema["properties"]["mode"]["enum"] == ["respond", "think", "ignore", "wait"]
    assert zone_chars_for(SUXIPO) == 2000


def test_zone_store_boot_render_and_edit(tmp_path):
    zone = ZoneStore(tmp_path / "zone.json")
    zone.boot("stone", scene=["我是匠石。", "路光把茶放到南窗桌上。"])
    assert zone.render("stone") == "B1  我是匠石。\nB2  路光把茶放到南窗桌上。"
    zone.apply_edit(
        "stone",
        [{"op": "del", "id": "B1"}, {"op": "mod", "id": "B2", "text": "路光把茶放到北窗。"}],
        append_text="我抬头。",
    )
    assert zone.get("stone") == ("路光把茶放到北窗。", "我抬头。")


def test_zone_store_value_narration_capped_and_protected():
    zone = ZoneStore()
    zone.boot("s", value="v" * 100, scene=["块一", "块二"], value_cap=50)
    assert zone.value_narration("s") == "v" * 50
    assert len(zone.value_narration("s")) == 50
    assert zone.render("s") == "B1  " + "v" * 50 + "\nB2  块一\nB3  块二"
    # 价值叙述始终是 B1；删 B1（价值）被忽略、保留；删 B2（第一个场景块）生效
    zone.apply_edit("s", [{"op": "del", "id": "B1"}, {"op": "del", "id": "B2"}], append_text="追加")
    assert zone.value_narration("s") == "v" * 50
    assert zone.get("s") == ("块二", "追加")


def test_zone_store_edit_does_not_auto_trim_head():
    zone = ZoneStore()
    zone.boot("s", scene=["一" * 10, "二" * 10, "三" * 10, "四" * 10])
    # 模型没给出 del：程序不自动裁头，全部保留 + 追加
    result = zone.apply_edit("s", [], append_text="五" * 10)
    assert len(result) == 5
    assert result[-1] == "五" * 10


def test_zone_store_edit_strips_leading_block_id_and_ignores_add_id():
    zone = ZoneStore()
    zone.boot("s", value="自我介绍", scene=["旧块"])
    result = zone.apply_edit(
        "s",
        [
            {"op": "add", "id": "B9", "text": "B3 河滩上的鹅卵石"},
            {"op": "mod", "id": "B2", "text": "B2 改过的旧块"},
        ],
        append_blocks=("lux说：“你好。”", "我说：“在呢。”"),
    )
    assert result == (
        "改过的旧块",
        "河滩上的鹅卵石",
        "lux说：“你好。”",
        "我说：“在呢。”",
    )


def test_zone_store_edit_ignores_bad_id():
    zone = ZoneStore()
    zone.boot("s", scene=["甲", "乙"])
    result = zone.apply_edit("s", [{"op": "del", "id": "B9"}], append_text="")
    assert result == ("甲", "乙")


def test_smith_instruction_uses_shared_edit_rules():
    from jshi.style import SMITH, SMITH_INSTRUCTION

    instruction = instruction_for(SMITH)
    assert instruction == SMITH_INSTRUCTION
    assert "不要填 id" in instruction
    assert '"edit": []' in instruction
    assert "本轮对方的话和你的回应由程序追加" in instruction
    assert "斯密斯" not in instruction


def test_zone_store_boot_does_not_overwrite_existing():
    zone = ZoneStore()
    zone.boot("s", value="自我介绍", scene=["河滩上的鹅卵石"])
    kept = zone.boot("s", value="新的自我介绍", scene=["整份重写"])
    assert kept == ("河滩上的鹅卵石",)
    assert zone.value_narration("s") == "自我介绍"
    replaced = zone.boot(
        "s", value="覆盖", scene=["新块"], replace=True
    )
    assert replaced == ("新块",)
    assert zone.value_narration("s") == "覆盖"


def test_persona_parse_maps_fields():
    skill = CognitionSkill(
        FakeModel(
            {
                "mode": "respond",
                "reply": "风再大，信也跑不了。",
                "action": "把镇纸压了压",
                "reason": "接住他这句",
                "edit": [{"op": "del", "id": "B2"}],
            }
        )
    )
    resp = skill.run(_request(persona_instruction=SUXIPO_INSTRUCTION, persona_schema=SUXIPO_SCHEMA))
    assert resp.response_plan.mode == "respond"
    assert resp.response_plan.verbal_text() == "风再大，信也跑不了。"
    assert resp.response_plan.embodied_text() == "把镇纸压了压"
    assert resp.response_plan.reason == "接住他这句"
    assert resp.zone_edit == ({"op": "del", "id": "B2"},)


def test_persona_silent_mode_drops_verbal():
    skill = CognitionSkill(
        FakeModel({"mode": "wait", "reply": "不该说", "action": "", "reason": "等对方", "edit": []})
    )
    resp = skill.run(_request(persona_instruction=SUXIPO_INSTRUCTION, persona_schema=SUXIPO_SCHEMA))
    assert resp.response_plan.mode == "wait"
    assert resp.response_plan.verbal_text() == ""


def test_boot_parse_maps_scene_only():
    skill = CognitionSkill(
        FakeModel({"scene": ["路光把茶放到桌上，说：趁热喝。"]})
    )
    resp = skill.run(
        _request(
            persona_instruction=SUXIPO_BOOT_INSTRUCTION,
            persona_schema={"type": "object"},
            boot=True,
        )
    )
    assert resp.scene == ("路光把茶放到桌上，说：趁热喝。",)
    assert resp.value_narration == ""
    assert resp.response_plan.mode == "think"


def test_style_pack_value_narration_is_per_persona_given_text():
    # 价值叙述是每人格给定的固定文本，不经模型生成
    from jshi.style import (
        SMITH,
        SUXIPO,
        StylePack,
        StylePackRegistry,
        builtin_packs,
    )

    registry = StylePackRegistry(builtin_packs())
    assert registry.resolve(SUXIPO).value_narration == ""
    registry.register(
        StylePack(
            pack_id=SMITH,
            display_name="斯密斯",
            value_narration="我是匠石，是一段程序。",
        )
    )
    assert registry.resolve(SMITH).value_narration == "我是匠石，是一段程序。"


class _PersonaModel:
    name = "fake"

    def generate(self, request: ModelRequest) -> ModelResponse:
        if getattr(request, "boot", False):
            payload = {"scene": ["路光把茶放到南窗桌上。"]}
        else:
            payload = {
                "mode": "respond",
                "reply": "风再大，信也跑不了。",
                "action": "把镇纸压了压",
                "reason": "接话",
                "edit": [],
            }
        return ModelResponse(
            text=json.dumps(payload, ensure_ascii=False), model=self.name
        )


def test_suxipo_end_to_end_boot_then_respond(tmp_path):
    from jshi.identity import IdentityProfile, IdentityRepository
    from jshi.recognition import ObjectProfile
    from jshi.skill import SkillModelPort
    from jshi.style import StylePackStore
    from jshi.subject import SubjectProcess, SubjectRepository

    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    process = SubjectProcess(
        repository,
        identities,
        SkillModelPort(CognitionSkill(_PersonaModel())),
        style_packs=StylePackStore(tmp_path / "style_pack.json"),
        zone_store=ZoneStore(tmp_path / "zone.json"),
    )
    process.profiles.create(
        ObjectProfile(object_id="OBJ-USER", label="user", source="test", status="confirmed")
    )
    process.style_packs.set("stone", SUXIPO)
    process.activity_ledger.append_external(
        "stone",
        actor_object_id="OBJ-USER",
        text_raw="木头素材。" * 250,
    )

    result = process.experience("stone", "茶凉了，还坐得住吗？", object_ref="user")
    blocks = process.zone_store.get("stone")
    assert result.response_plan.mode == "respond"
    # 内置苏西坡.value_narration 留空（接口），故 boot 没放价值块
    assert process.zone_store.value_narration("stone") == ""
    assert any("路光把茶放到南窗桌上" in b for b in blocks)
    assert blocks[-2] == "user说：“茶凉了，还坐得住吗？”"
    assert blocks[-1].startswith("我说：")


class _CountingPersonaModel(_PersonaModel):
    def __init__(self) -> None:
        self.boot_calls = 0
        self.other_calls = 0

    def generate(self, request: ModelRequest) -> ModelResponse:
        if getattr(request, "boot", False):
            self.boot_calls += 1
        else:
            self.other_calls += 1
        return super().generate(request)


def test_existing_zone_is_not_rebooted(tmp_path):
    from jshi.identity import IdentityProfile, IdentityRepository
    from jshi.recognition import ObjectProfile
    from jshi.skill import SkillModelPort
    from jshi.style import SMITH, StylePackStore
    from jshi.subject import SubjectProcess, SubjectRepository

    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    model = _CountingPersonaModel()
    zone = ZoneStore(tmp_path / "zone.json")
    zone.boot("stone", value="我是匠石。", scene=["河滩上的鹅卵石还在。", "他说过哈喽啊。"])
    process = SubjectProcess(
        repository,
        identities,
        SkillModelPort(CognitionSkill(model)),
        style_packs=StylePackStore(tmp_path / "style_pack.json"),
        zone_store=zone,
    )
    process.profiles.create(
        ObjectProfile(object_id="OBJ-USER", label="user", source="test", status="confirmed")
    )
    process.style_packs.set("stone", SMITH)

    result = process.experience("stone", "有别的比较强的证据吗", object_ref="user")
    blocks = process.zone_store.get("stone")
    assert result.response_plan.mode == "respond"
    assert model.boot_calls == 0
    assert model.other_calls == 1
    assert process.zone_store.value_narration("stone") == "我是匠石。"
    assert blocks[0] == "河滩上的鹅卵石还在。"
    assert blocks[1] == "他说过哈喽啊。"
    assert blocks[-2] == "user说：“有别的比较强的证据吗”"
    assert blocks[-1].startswith("我说：")
