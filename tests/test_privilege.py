"""超级权限对象、密码登录与提示词规则写入。"""

from __future__ import annotations

from jshi.app.talk_session import TalkSession
from jshi.core import Provenance, SubjectState
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse, build_system
from jshi.privilege import PromptRuleStore, SuperPermissionStore
from jshi.recognition import ObjectProfile, new_object_id
from jshi.subject import SubjectProcess, SubjectRepository


class CaptureModel:
    name = "capture"

    def __init__(self) -> None:
        self.request: ModelRequest | None = None

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.request = request
        return ModelResponse(text="回应", model=self.name)


def _runtime(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石。"))
    repository = SubjectRepository(tmp_path / "subject.sqlite3")
    model = CaptureModel()
    prompt_rules = PromptRuleStore(tmp_path / "prompt_rules.json")
    process = SubjectProcess(repository, identities, model, prompt_rules=prompt_rules)
    object_id = new_object_id()
    process.profiles.create(
        ObjectProfile(object_id=object_id, label="lux", source="test", status="confirmed")
    )
    super_permissions = SuperPermissionStore(tmp_path / "super_permissions.json")
    super_permissions.grant(object_id, "lux", "pw-123")
    return process, model, object_id, super_permissions, prompt_rules


def test_super_permission_store_verify_and_revoke(tmp_path):
    store = SuperPermissionStore(tmp_path / "super.json")
    store.grant("OBJ-A", "lux", "pw-123")

    assert store.is_super("OBJ-A")
    assert store.verify("OBJ-A", "pw-123")
    assert not store.verify("OBJ-A", "wrong")
    assert not store.verify("OBJ-B", "pw-123")

    store.revoke("OBJ-A")
    assert not store.is_super("OBJ-A")


def test_prompt_rule_store_add_list_remove(tmp_path):
    store = PromptRuleStore(tmp_path / "rules.json")
    rule = store.add("stone", "被质疑要先回答", section="回应方式", source="lux")

    assert store.list("stone") == (rule,)

    store.remove(rule.id)
    assert store.list("stone") == ()


def test_build_system_renders_governing_rules():
    state = SubjectState(
        subject_id="stone",
        identity_summary="匠石",
        current_stance="诚实",
        provenance=Provenance(source="test"),
    )
    request = ModelRequest(
        purpose="subject_activity",
        input_text="你好",
        subject_state=state,
        system_extra="【关于你】\n你是匠石。",
        governing_rules=("被质疑要先回答", "[回应方式] 先承认再说明"),
    )

    system = build_system(request)

    assert "【附加规则】" in system
    assert "被质疑要先回答" in system
    assert "[回应方式] 先承认再说明" in system


def test_talk_session_login_gates_privileged_commands(tmp_path):
    process, _model, _object_id, super_permissions, prompt_rules = _runtime(tmp_path)
    session = TalkSession(
        process,
        data_dir=tmp_path,
        subject_id="stone",
        speaker="lux",
        channel=None,
        carriers=(),
        super_permissions=super_permissions,
    )

    denied = session.handle("/rule 要诚实")
    assert "需要超级权限" in denied.events[0].text

    wrong = session.handle("/login nope")
    assert "密码错误" in wrong.events[0].text

    ok = session.handle("/login pw-123")
    assert "已登录" in ok.events[0].text

    rule_out = session.handle("/rule 回应方式 被质疑要先回答")
    assert "已写入提示词规则" in rule_out.events[0].text
    assert prompt_rules.list("stone")[0].section == "回应方式"

    value_out = session.handle("/value 与朋友交，言而有信")
    assert "已写入value" in value_out.events[0].text


def test_subject_process_injects_governing_rules_into_model_request(tmp_path):
    process, model, _object_id, _super_permissions, prompt_rules = _runtime(tmp_path)
    prompt_rules.add("stone", "被质疑要先回答", section="回应方式", source="lux")

    process.experience("stone", "你好", object_ref="lux")

    assert model.request is not None
    assert model.request.governing_rules == ("[回应方式] 被质疑要先回答",)
