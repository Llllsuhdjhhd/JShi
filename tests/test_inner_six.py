import json
from dataclasses import replace
from datetime import timedelta

import pytest

from jshi.reflection import IntrospectionLevel, IntrospectionRequest, IntrospectionTrigger
from jshi.core.param_overlay import override_snapshot
from jshi.subject import CognitiveContent, CognitiveKind, EpistemicStatus, EvidenceKind
from test_introspection import IntrospectionModel, NOW, SIX_ANSWERS, runtime


def question(**evidence):
    return IntrospectionRequest("stone", IntrospectionTrigger.IDLE, "recent", NOW,
        entry_text="对方提出的习惯，和我实际做过的事有什么联系？",
        origin="discovery", object_id="person-original-id", evidence=evidence)


def test_six_collects_anchored_original_and_result_without_registering_a_recall(tmp_path):
    process, _ = runtime(tmp_path)
    ledger = process.activity_ledger
    original = ledger.append_external("stone", actor_object_id="person-original-id",
        text_raw="对方总是先问预估时间", occurred_at=NOW - timedelta(days=4))
    ledger.append_external("stone", actor_object_id="another-person-id",
        text_raw="没有被本题选中的交谈", occurred_at=NOW)
    request = question(segment_ids=[original.segment_id, "missing-id"], reason="后来注意到两次预估不同",
        baseline_status="unknown", baseline_sample_count=0,
        tool_facts=[dict(source_id="tool-private-id", task_id="task-private-id",
                         status="超时", summary="比承诺的时间长", at=NOW.isoformat())])
    snapshot = process.reflection.collect_question(request)
    text = snapshot["text"]
    assert all(word in text for word in ("先问预估时间", "后来注意到", "比承诺的时间长", "[E1]", "[T1]", "P1"))
    assert all(word not in text for word in (original.segment_id, "没有被本题选中", "private-id", "missing-id", "person-original-id"))
    assert snapshot["missing"] == ["missing-id"]
    assert snapshot["references"]["E1"][0] == original.segment_id
    assert snapshot["references"]["T1"][0] == "tool-private-id"
    assert "没有可对照的样本" in text
    assert process.reflection.prepare_question(request, collected=snapshot)["state"] == "waiting"


def test_original_revised_cognition_survives_absent_neighboring_turns(tmp_path):
    process, repo = runtime(tmp_path)
    cognition = CognitiveContent(subject_id="stone", kind=CognitiveKind.EVALUATION, activity_id="old",
        content="以前我认为预估的时间总是足够", epistemic_status=EpistemicStatus.CONSIDERING,
        evidence_kind=EvidenceKind.COGNITIVE_REASONING)
    repo.add_cognitive_content(cognition)
    ref = "cognition:" + cognition.id
    request = replace(question(reason="对方实际等了更久"), trigger=IntrospectionTrigger.EPISTEMIC_REVISED,
                      entry_ref=ref)
    material = process.reflection.collect_question(request)
    assert material["references"]["E1"] == [ref, cognition.content, None]
    assert cognition.id not in material["text"] and "此前形成的认识" in material["text"]


def test_reopened_question_distinguishes_previous_hypothesis_and_tool_evidence(tmp_path):
    process, _ = runtime(tmp_path)
    request = question(previous_open_question="上次是不是高估了能力？", previous_waiting_for="下次的实际返回",
        tool_facts=[{"source_id": "tool-private-a", "status": "ok",
                     "actual_result": {"value": 23, "unit": "秒", "trace_id": "private-trace"}},
                    {"source_id": "tool-private-b", "status": "ok", "actual_result": {"log": "private-log"}}])
    material = process.reflection.collect_question(request)
    topic, evidence = material["text"].split("【可核对的现场】")
    assert "上次是不是高估了能力？" in topic and "下次的实际返回" in topic
    assert "上次是不是高估了能力？" not in evidence
    assert "value=23" in evidence and "没有呈现可核对的返回值" in evidence and "private" not in evidence


def test_recall_question_validates_originals_and_citations_and_preserves_pending(tmp_path, monkeypatch):
    payload = dict(SIX_ANSWERS, interaction_need={"kind": "ask", "object_id": "P1"},
        open_question="一次失误是不是长期习惯？", waiting_for="下次使用记录",
        candidates=[{"content": "一次失误是不是长期习惯？", "source_refs": ["M1"]},
                    {"content": "加一个未看过的片段", "source_refs": ["M8"]},
                    {"content": "先记录预估时间，再核对结果", "source_refs": ["M1"]}])
    model = IntrospectionModel(payload=payload)
    process, _ = runtime(tmp_path, model=model)
    request = replace(question(recall_events=[
        dict(event_id="original-id", content="我说半分钟，实际花了两分钟", query_object_role="involved",
             object_id="person-original-id", occurred_at=None),
        dict(event_id="insight:forbidden", content="已有认识"),
        dict(event_id="expired", validity="superseded", content="已经被纠正")]), origin="recall_reprocess")
    engine = process.reflection
    monkeypatch.setattr(engine.memory, "recall", lambda *a, **kw: pytest.fail("recursive recall"))
    material = engine.collect_question(request)
    assert material["references"] == {"M1": ["original-id", "我说半分钟，实际花了两分钟", None]}
    assert "原事件 ?" in material["text"] and "已有认识" not in material["text"]
    frozen = engine.prepare_question(request, collected=json.loads(json.dumps(material)))
    assert model.purposes.count("introspection") == 1
    assert frozen["state"] == "waiting"
    assert frozen["coverage"]["model_visible_refs"] == ["M1"]
    assert frozen["answer"]["interaction_need"] is None
    assert [item["content"] for item in frozen["answer"]["candidates"]] == ["先记录预估时间，再核对结果"]
    assert [item["source_ids"] for item in frozen["proposals"]] == [["original-id"]]
    assert "person_scope" not in frozen["proposals"][0]

    # A follow-up is authoritative only when explicitly attached, and cannot replace the original M.
    later = process.activity_ledger.append_external("stone", actor_object_id="speaker",
        text_raw="这次确实先查了用时", occurred_at=NOW + timedelta(days=2))
    evidence = dict(request.evidence, segment_ids=[later.segment_id])
    linked = replace(request, evidence=evidence)
    assert set(engine.collect_question(linked)["references"]) == {"E1", "M1"}
    missing = replace(request, evidence=dict(evidence, recall_events=[]))
    assert engine.prepare_question(missing)["state"] == "waiting"
    assert model.purposes.count("introspection") == 1


def test_low_cost_answer_and_durable_history_are_replayable(tmp_path, monkeypatch):
    model = IntrospectionModel()
    process, repo = runtime(tmp_path, model=model)
    original = process.activity_ledger.append_external("stone", actor_object_id="speaker",
        text_raw="下次想先问预估时间", occurred_at=NOW)
    request = replace(question(segment_ids=[original.segment_id]), level=IntrospectionLevel.LOW)
    engine = process.reflection
    frozen = json.loads(json.dumps(engine.prepare_question(request)))
    assert frozen["answer"]["mode"] == "rule" and not model.purposes
    assert frozen["coverage"]["model_visible_refs"] == []

    add = repo.add_history
    def fail_on_completion(record, **kw):
        if record.event_type == "introspection_done":
            raise RuntimeError("simulate interrupted publication")
        return add(record, **kw)

    with monkeypatch.context() as mp:
        mp.setattr(repo, "add_history", fail_on_completion)
        with pytest.raises(RuntimeError, match="interrupted publication"):
            engine.publish_question(request, frozen, job_key="same-revision")
    assert not repo.list_activities("stone") and not repo.list_history("stone")
    first = engine.publish_question(request, frozen, job_key="same-revision")
    second = engine.publish_question(request, frozen, job_key="same-revision")
    assert first.activity_id == second.activity_id
    assert first.cognitive_content_id == second.cognitive_content_id
    assert len(repo.list_transitions(first.activity_id)) == 1
    rows = [r for r in repo.list_history("stone") if r.event_type == "introspection_done"]
    assert len(rows) == 1 and rows[0].content["request_id"] == request.request_id
    assert repo.get_cognitive_content(first.cognitive_content_id).source_ids == (original.segment_id,)


def test_followup_records_stages_without_turning_suggestions_into_results(tmp_path, monkeypatch, capsys):
    import sys
    from jshi.app import cli

    with override_snapshot({"INNER_ACTIVITY_MODE": 2}):
        model = IntrospectionModel()
        process, repo = runtime(tmp_path, model=model)
        inner = process.reflection_executor.inner
        ledger = process.activity_ledger
        old = ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="上次等了很久")
        result = process.reflect("stone", "以后询问时间时该怎样估时？")
        report = inner.questions.followups("stone", result.id)
        assert set(report["suggestions"]) >= {"tool", "next_time"} and not report["observations"]
        with pytest.raises(ValueError, match="建议形成以后"):
            inner.revisit("stone", result.id, old.segment_id, stage="outcome", suggestion="tool")
        evidence = ledger.append_external("stone", actor_object_id="OBJ-USER", text_raw="你刚才先估时，随后准时回了我。")
        with pytest.raises(ValueError, match="指定"):
            inner.revisit("stone", result.id, evidence.segment_id, stage="adopted")
        args = ["jshi", "--data-dir", str(tmp_path), "introspect-followup", "stone", result.id]
        monkeypatch.setattr(cli, "_runtime", lambda *a, **kw: (process, None, repo))
        monkeypatch.setattr(sys, "argv", args + [evidence.segment_id, "--stage", "adopted", "--suggestion", "next_time"])
        cli.main()
        assert "实际采用" in capsys.readouterr().out
        for stage in ("adopted", "outcome", "outcome"):
            process.revisit_introspection("stone", result.id, evidence.segment_id, stage=stage, suggestion="next_time")
        monkeypatch.setattr(sys, "argv", args)
        cli.main()
        report = json.loads(capsys.readouterr().out)
        assert [item["stage"] for item in report["observations"]] == ["adopted", "outcome"]
        assert all(item["suggestion_cognition_id"] == result.id and item["segment"] == evidence.segment_id
                   for item in report["observations"])
        assert model.purposes == ["introspection"]
        assert len(process.drain_introspection("stone", limit=4)) == 1
        assert model.purposes == ["introspection", "introspection"]


def test_only_concrete_suggestion_can_receive_an_outcome(tmp_path):
    with override_snapshot({"INNER_ACTIVITY_MODE": 2}):
        model = IntrospectionModel(payload=dict(SIX_ANSWERS, next_time="", interaction_need=None,
            tool_need=False, worth_learning=False, candidates=[]))
        process, _ = runtime(tmp_path, model=model)
        origin = process.reflect("stone", "还有什么待证实？")
        segment = process.activity_ledger.append_external("stone", actor_object_id="speaker", text_raw="材料到了")
        with pytest.raises(ValueError, match="可用建议：无"):
            process.revisit_introspection("stone", origin.id, segment.segment_id, stage="outcome")
        assert not process.reflection_executor.inner.questions.followups("stone", origin.id)["observations"]
        assert not process.drain_introspection("stone")
