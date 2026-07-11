from __future__ import annotations

from dataclasses import replace

import pytest

from jshi.chance import SeededAttentionPolicy
from jshi.core import (
    CandidateChange,
    Event,
    EventKind,
    Provenance,
    TruthStatus,
)
from jshi.governance import GovernanceService
from jshi.identity import IdentityProfile, IdentityRepository, SimpleSelfPort
from jshi.infrastructure import PluginRegistry, SQLiteEventStore
from jshi.inner import InnerActivity, InnerActivityKind
from jshi.mind import MindDomain, PlaceholderMindPlugin, default_mind_plugins
from jshi.models import EchoModel
from jshi.orchestration import Orchestrator


def runtime(tmp_path):
    identities = IdentityRepository(tmp_path / "identities.json")
    identities.create(
        IdentityProfile(
            subject_id="stone",
            name="匠石",
            origin="测试基础型",
            narrative="我是匠石。",
        )
    )
    store = SQLiteEventStore(tmp_path / "events.sqlite3")
    plugins = PluginRegistry(default_mind_plugins())
    orchestrator = Orchestrator(
        event_store=store,
        identities=identities,
        self_port=SimpleSelfPort(),
        plugins=plugins,
        model=EchoModel(),
        governance=GovernanceService(),
        chance=SeededAttentionPolicy(7),
    )
    return orchestrator, identities, store, plugins


def test_external_and_inner_cycles_have_distinct_provenance(tmp_path):
    orchestrator, identities, store, _ = runtime(tmp_path)

    external = orchestrator.interact("stone", "你好")
    inner = orchestrator.run_inner(
        "stone",
        InnerActivity(
            kind=InnerActivityKind.DREAM,
            prompt="把近期印象重新组合",
            allow_imagination=True,
        ),
    )

    assert external.input_event.truth_status is TruthStatus.REPORTED
    assert external.output_event.kind is EventKind.ACTION
    assert inner.input_event.kind is EventKind.INNER_ACTIVITY
    assert inner.output_event.truth_status is TruthStatus.IMAGINED
    assert identities.get("stone").revision == 1
    assert len(store.list_for_subject("stone")) >= 6


def test_raw_event_is_append_only_and_derivation_is_separate(tmp_path):
    _, _, store, _ = runtime(tmp_path)
    original = Event(
        subject_id="stone",
        kind=EventKind.EXTERNAL_INPUT,
        content={"text": "原始内容"},
        truth_status=TruthStatus.REPORTED,
        provenance=Provenance(source="test"),
    )
    store.append(original)

    with pytest.raises(Exception):
        store.append(replace(original, content={"text": "覆盖"}))

    loaded = store.get(original.id)
    assert loaded is not None
    assert loaded.content["text"] == "原始内容"


def test_plugin_and_model_seams_do_not_change_identity(tmp_path):
    orchestrator, identities, _, plugins = runtime(tmp_path)
    before = identities.get("stone")

    previous = plugins.replace(
        PlaceholderMindPlugin(
            MindDomain.MEMORY,
            initial_context={"concerns": ["新的记忆实现"]},
        )
    )
    orchestrator.model = EchoModel()
    result = orchestrator.interact("stone", "测试替换")

    assert previous is not None
    assert result.subject_state.subject_id == "stone"
    assert identities.get("stone") == before


def test_governance_rejects_imagined_fact_and_weak_identity_change():
    governance = GovernanceService()
    imagined_fact = CandidateChange(
        subject_id="stone",
        target="fact",
        operation="add",
        value={"truth_status": TruthStatus.IMAGINED.value},
        provenance=Provenance(source="dream"),
    )
    weak_identity = CandidateChange(
        subject_id="stone",
        target="identity",
        operation="revise",
        value={"narrative": "突然成为另一个主体"},
        provenance=Provenance(source="conversation"),
        significance=0.2,
    )

    decision = governance.review((imagined_fact, weak_identity))
    assert decision.accepted == ()
    assert len(decision.rejected) == 2


def test_identity_persists_across_repository_instances(tmp_path):
    path = tmp_path / "identities.json"
    first = IdentityRepository(path)
    first.create(IdentityProfile("stone", "匠石", "测试"))

    second = IdentityRepository(path)
    assert second.get("stone").name == "匠石"
