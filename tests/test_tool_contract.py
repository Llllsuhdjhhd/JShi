"""工具使用契约 / 模块的最小测试（用 StubEngine，确定性）。

真引擎（PiEngine）为 live 适配，不在此覆盖。
"""

from __future__ import annotations

from jshi.tool import (
    AskMode,
    Budget,
    CreateToolSpec,
    FeedbackKind,
    PermissionContext,
    PiEngine,
    StubEngine,
    ToolModule,
    ToolOrigin,
    ToolRequest,
    ToolStatus,
    engine_tools,
)
from jshi.tool.port import ToolUseOutcome


def _request(**overrides) -> ToolRequest:
    base = dict(subject_id="stone", need="查一下天气", template="echo")
    base.update(overrides)
    return ToolRequest(**base)


def test_request_defaults() -> None:
    req = ToolRequest(subject_id="stone", need="查天气")
    assert req.subject_id == "stone"
    assert req.need == "查天气"
    assert req.origin is ToolOrigin.EXTERNAL_05
    assert req.ask is AskMode.EXECUTE
    assert req.request_id
    assert req.turn == 0
    assert req.params == {}
    assert req.budget.cost_cap is None


def test_request_origin_introspection() -> None:
    req = ToolRequest(subject_id="stone", need="想想", origin=ToolOrigin.INTROSPECTION)
    assert req.origin is ToolOrigin.INTROSPECTION


def test_budget_and_permission() -> None:
    req = ToolRequest(
        subject_id="stone",
        need="跑代码",
        budget=Budget(cost_cap=1.0, timeout_ms=5000, max_tokens=2000),
        permission=PermissionContext(
            allow=("read",), deny=("write",), require_confirm=True
        ),
    )
    assert req.budget.cost_cap == 1.0
    assert req.budget.timeout_ms == 5000
    assert req.budget.max_tokens == 2000
    assert req.permission.allow == ("read",)
    assert req.permission.deny == ("write",)
    assert req.permission.require_confirm is True


def test_stub_feedback_stream_order() -> None:
    outcome = ToolModule(StubEngine()).submit(_request())
    kinds = [item.kind for item in outcome.feedback]
    assert kinds == [
        FeedbackKind.PROGRESS,
        FeedbackKind.RESULT,
    ]


def test_engine_does_not_yield_estimate() -> None:
    outcome = ToolModule(StubEngine()).submit(_request())
    assert outcome.estimate is None


def test_engine_tools_from_stub() -> None:
    items = engine_tools(StubEngine())
    assert items[0]["name"] == "echo"
    assert items[0]["description"]


def test_stub_result_echoes_need() -> None:
    outcome = ToolModule(StubEngine()).submit(_request(need="hello"))
    assert outcome.result.status is ToolStatus.OK
    assert outcome.result.result["echo"] == "hello"
    assert outcome.result.summary.startswith("echo:")


def test_stub_propose_only_rejected() -> None:
    outcome = ToolModule(StubEngine()).submit(_request(ask=AskMode.PROPOSE_ONLY))
    assert outcome.result.status is ToolStatus.REJECTED


def test_stub_never_claims_a_real_result() -> None:
    """占位引擎必须自曝：它只回显需求，从不真的办事。

    2026-09-13：实测里 stub 回显被包装写成「已经查到具体数值」，下游因此
    对着一个不存在的结论继续说话。所以 ideal 必须为假、原因要写在 ideal_note。
    """
    outcome = ToolModule(StubEngine()).submit(_request(need="杭州天气"))
    result = outcome.result
    assert result.status is ToolStatus.OK       # 契约不变：引擎层面仍是「跑完了」
    assert result.ideal is False                # 但没有真实产出
    assert "占位" in result.ideal_note
    assert result.summary.startswith("echo:")
    assert "不是真实结果" in result.summary


def test_stub_catalog_marks_itself_as_placeholder() -> None:
    """目录里每条都要带 source=stub 与「占位」字样，免得被 205 当成真工具。"""
    engine = StubEngine()
    listed = engine.list_commands()
    assert listed[0]["source"] == "stub"
    assert "占位" in listed[0]["description"]

    ToolModule(engine).submit(
        _request(ask=AskMode.CREATE_TOOL, create=CreateToolSpec(tool_name="weather"))
    )
    created = engine.list_commands()[-1]
    assert created["name"] == "weather"
    assert created["source"] == "stub"
    assert "占位实现" in created["description"]


def test_engine_catalog() -> None:
    engine = StubEngine()
    assert engine.name == "stub"
    assert engine.list_templates() == ("echo",)
    assert engine.list_commands()[0]["name"] == "echo"


def test_tool_module_uses_engine_catalog() -> None:
    module = ToolModule(StubEngine(tool_name="ping"))
    assert module.list_templates() == ("ping",)
    assert module.engine_name == "stub"


def test_feedback_envelope_meta_and_version() -> None:
    outcome = ToolModule(StubEngine()).submit(
        _request(meta={"task": "demo"})
    )
    for item in outcome.feedback:
        assert item.version == "v1"
        assert item.request_id == outcome.request_id
    assert outcome.estimate is None
    assert outcome.progress
    assert outcome.result_feedback is not None


def test_outcome_helpers() -> None:
    req = _request()
    outcome: ToolUseOutcome = ToolModule(StubEngine()).submit(req)
    assert outcome.request_id == req.request_id
    assert outcome.estimate is None
    assert all(item.kind is FeedbackKind.PROGRESS for item in outcome.progress)
    assert outcome.result.status is ToolStatus.OK
    assert outcome.result_feedback is not None and outcome.result_feedback.kind is FeedbackKind.RESULT


class _BoomListEngine:
    name = "boom"

    def list_commands(self):
        raise FileNotFoundError("nope")


def test_engine_tools_swallows_listing_errors() -> None:
    assert engine_tools(_BoomListEngine()) == ()
    assert engine_tools(PiEngine(executable="pi-does-not-exist-xyz")) == ()
