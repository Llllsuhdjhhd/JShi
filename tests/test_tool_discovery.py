"""205 引擎命令检索：分档披露、BM25、策划短循环。"""

from __future__ import annotations

import json

from jshi.models import ModelRequest, ModelResponse, ResponseItem, ResponsePlan
from jshi.skill import SkillPlanner, ToolPlanSkill
from jshi.tool.catalog import load_catalog
from jshi.tool.contract import ToolRequest
from jshi.tool.discovery import (
    DIRECT_MAX_TOOLS,
    LISTING_MAX_CHARS,
    LOOKUP_MAX_ROUNDS,
    TIER_EAGER,
    TIER_INDEX,
    TIER_LISTING,
    TIER_NAMES,
    ToolIndex,
    tokenize,
)
from jshi.tool.intake import IntakeRecord
from jshi.tool.plan import PlanFailure, ToolPlan


class _JsonPort:
    name = "json-port"

    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = 0
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.calls += 1
        if callable(self.payload):
            text = self.payload(self.calls, request)
        elif isinstance(self.payload, str):
            text = self.payload
        else:
            text = json.dumps(self.payload, ensure_ascii=False)
        return ModelResponse(
            model=self.name,
            response_plan=ResponsePlan(
                mode="respond",
                items=(ResponseItem(channel="verbal", text=text),),
            ),
            raw_text=text,
        )


def _intake(need: str = "查杭州明天的天气") -> IntakeRecord:
    return IntakeRecord(
        intake_id="in-1",
        subject_id="stone",
        object_id="OBJ-A",
        need=need,
    )


def _pad(text: str, width: int) -> str:
    return (text + ("x" * width))[:width]


def _many_send(n: int, *, desc_len: int = 180) -> list[dict]:
    items = []
    for i in range(n):
        items.append(
            {
                "name": f"send_note_{i:03d}",
                "description": _pad("send a short note to a mailbox ", desc_len),
                "params": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "body": {"type": "string"},
                    },
                },
            }
        )
    return items


def _weather() -> dict:
    return {
        "name": "get_weather",
        "description": "query weather forecast by city name 查询天气 预报",
        "params": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "city to query"},
            },
        },
    }


def _big_catalog(n: int = 80, *, desc_len: int = 180) -> tuple[dict, ...]:
    return tuple(_many_send(n, desc_len=desc_len) + [_weather()])


def test_small_catalog_is_eager() -> None:
    index = ToolIndex.from_maps(({"name": "echo", "description": "回显"},))
    assert index.tier == TIER_EAGER
    assert index.is_eager
    assert index.total == 1


def test_large_catalog_is_not_eager() -> None:
    index = ToolIndex.from_maps(_big_catalog())
    assert index.total > DIRECT_MAX_TOOLS
    assert index.tier != TIER_EAGER
    assert not index.is_eager


def test_listing_falls_back_to_names_when_descriptions_overflow() -> None:
    index = ToolIndex.from_maps(_big_catalog(80, desc_len=180))
    assert index.tier == TIER_NAMES
    listing = index.disclose().listing
    assert listing
    assert "description" not in listing[0]
    dumped = json.dumps(listing, ensure_ascii=False, separators=(",", ":"))
    assert len(dumped) <= LISTING_MAX_CHARS


def test_short_descriptions_keep_listing_tier() -> None:
    tools = tuple(
        {"name": f"tool_{i:02d}", "description": "short"}
        for i in range(12)
    ) + (_weather(),)
    index = ToolIndex.from_maps(tools)
    assert index.tier == TIER_LISTING
    assert "description" in index.disclose().listing[0]


def test_index_tier_when_names_overflow() -> None:
    tools = tuple(
        {"name": f"tool_with_a_very_long_identifier_{i:04d}", "description": "x"}
        for i in range(400)
    )
    index = ToolIndex.from_maps(tools)
    assert index.tier == TIER_INDEX
    assert index.disclose().listing == ()
    assert index.disclose().sources


def test_search_rarest_token_keeps_weather_not_send_notes() -> None:
    index = ToolIndex.from_maps(_big_catalog())
    result = index.search("send weather")
    names = [item["name"] for item in result["hits"]]
    assert names == ["get_weather"]
    assert result["rarest"] in tokenize("weather")


def test_search_chinese_need_ignores_place_oov() -> None:
    index = ToolIndex.from_maps(_big_catalog())
    result = index.search("杭州天气")
    names = [item["name"] for item in result["hits"]]
    assert "get_weather" in names
    assert all(not name.startswith("send_note_") for name in names)


def test_search_all_oov_is_empty() -> None:
    index = ToolIndex.from_maps(_big_catalog())
    result = index.search("珠穆朗玛")
    assert result["hits"] == []
    assert result["hint"]


def test_search_empty_query_is_empty() -> None:
    index = ToolIndex.from_maps(_big_catalog())
    result = index.search("   ")
    assert result["hits"] == []


def test_describe_returns_full_schema_and_missing() -> None:
    index = ToolIndex.from_maps(_big_catalog())
    result = index.describe(["get_weather", "no-such", "get_weather"])
    assert result["not_found"] == ["no-such"]
    assert len(result["tools"]) == 1
    tool = result["tools"][0]
    assert tool["name"] == "get_weather"
    assert "weather" in tool["description"]
    assert "city" in json.dumps(tool.get("params") or {}, ensure_ascii=False)


def test_eager_plan_payload_still_dumps_engine_tools() -> None:
    port = _JsonPort({"ok": True, "template": "generic", "command": "echo"})
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=({"name": "echo", "description": "回显"},),
    )
    planned = planner.plan(_intake())
    assert isinstance(planned, ToolRequest)
    payload = json.loads(port.requests[0].input_text)
    assert "engine_tools" in payload
    assert "discovery" not in payload
    assert payload["engine_tools"][0]["name"] == "echo"


def test_plan_fills_missing_estimate_from_observed() -> None:
    tool = {
        "name": "get_weather",
        "description": "query weather",
        "meta": {
            "observed": {
                "runs": 4,
                "avg_time_ms": 1234,
                "avg_cost": 0.12,
                "avg_tokens": 210,
            }
        },
    }
    port = _JsonPort(
        {
            "ok": True,
            "command": "get_weather",
            "params": {},
            "estimate": {"time_est_ms": None, "cost_est": None},
        }
    )
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=(tool,),
    )
    planned = planner.plan(_intake())
    assert isinstance(planned, ToolRequest)
    estimate = planned.meta["estimate"]
    assert estimate["time_est_ms"] == 1234
    assert estimate["cost_est"] == 0.12
    assert estimate["observed_runs"] == 4


def test_plan_create_tool_preserves_feedback_plan() -> None:
    port = _JsonPort(
        {
            "ok": True,
            "ask": "create_tool",
            "create_tool": {
                "tool_name": "book_flight",
                "tool_intent": "查询并预订机票",
                "params_schema": {"from": {"type": "string"}},
                "feedback_plan": {
                    "stages": ["search", "rank", "book"],
                    "result_fields": ["flight_no", "price"],
                },
            },
        }
    )
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=(),
    )
    planned = planner.plan(_intake("订一张机票"))
    assert isinstance(planned, ToolRequest)
    assert planned.create is not None
    assert planned.create.feedback_plan["stages"] == ["search", "rank", "book"]
    assert planned.create.feedback_plan["result_fields"] == ["flight_no", "price"]


def test_plan_skill_outputs_multi_step_plan() -> None:
    tools = (
        {"name": "get_weather", "description": "query weather"},
        {"name": "get_flight", "description": "query flight"},
    )
    port = _JsonPort(
        {
            "ok": True,
            "plan": {
                "mode": "parallel",
                "steps": [
                    {
                        "step_id": "weather",
                        "command": "get_weather",
                        "params": {"city": "上海"},
                    },
                    {
                        "step_id": "flight",
                        "command": "get_flight",
                        "params": {"to": "上海"},
                    },
                ],
            },
        }
    )
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=tools,
    )
    planned = planner.plan(_intake())
    assert isinstance(planned, ToolPlan)
    assert planned.mode == "parallel"
    assert [step.step_id for step in planned.steps] == ["weather", "flight"]


def test_plan_skill_rejects_missing_dependency() -> None:
    tools = ({"name": "echo", "description": "回显"},)
    port = _JsonPort(
        {
            "ok": True,
            "plan": {
                "mode": "sequential",
                "steps": [
                    {
                        "step_id": "a",
                        "command": "echo",
                        "depends_on": ["missing"],
                    }
                ],
            },
        }
    )
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=tools,
    )
    planned = planner.plan(_intake())
    assert isinstance(planned, PlanFailure)


def test_plan_skill_accepts_agent_loop_mode() -> None:
    tools = ({"name": "echo", "description": "回显"},)
    port = _JsonPort(
        {
            "ok": True,
            "plan": {
                "mode": "agent_loop",
                "steps": [
                    {
                        "step_id": "a",
                        "command": "echo",
                        "params": {},
                    },
                    {
                        "step_id": "b",
                        "command": "echo",
                        "depends_on": ["a"],
                    },
                ],
            },
        }
    )
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=tools,
    )
    planned = planner.plan(_intake())
    assert isinstance(planned, ToolPlan)
    assert planned.mode == "agent_loop"


def test_planner_surfaces_unavailable_engine() -> None:
    class _UnavailableEngine:
        name = "pi"
        _spawn_error = "boom"
        _last_stop = ""

        def list_commands(self):
            return ()

    planner = SkillPlanner(
        ToolPlanSkill(_JsonPort({"ok": True, "command": "anything"})),
        catalog=load_catalog(),
        engine=_UnavailableEngine(),
    )
    planned = planner.plan(_intake())
    assert isinstance(planned, PlanFailure)
    assert "连不上工具引擎" in planned.message


def test_planner_ignores_stale_execute_timeout() -> None:
    """上一次执行超时留下的 _last_stop 不得挡下一轮策划。"""

    class _OkEngine:
        name = "pi"
        _spawn_error = ""
        _last_stop = "timeout"

        def list_commands(self):
            return ({"name": "echo", "description": "echo"},)

    planner = SkillPlanner(
        ToolPlanSkill(
            _JsonPort(
                {
                    "ok": True,
                    "ask": "execute",
                    "command": "echo",
                    "params": {},
                    "estimate": {},
                }
            )
        ),
        catalog=load_catalog(),
        engine=_OkEngine(),
    )
    planned = planner.plan(_intake())
    assert not isinstance(planned, PlanFailure)
    assert getattr(planned, "command", "") == "echo"


def test_observed_meta_is_available_on_describe() -> None:
    index = ToolIndex.from_maps(
        (
            {
                "name": "get_weather",
                "description": "query weather",
                "meta": {"observed": {"runs": 2, "avg_time_ms": 900}},
            },
        )
    )
    described = index.describe(["get_weather"])
    assert described["tools"][0]["meta"]["observed"]["avg_time_ms"] == 900


def test_large_catalog_first_payload_does_not_dump_all_params() -> None:
    catalog = _big_catalog()
    port = _JsonPort({"ok": True, "template": "generic", "command": "get_weather"})
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=catalog,
    )
    planned = planner.plan(_intake())
    assert isinstance(planned, ToolRequest)
    assert planned.command == "get_weather"
    payload = json.loads(port.requests[0].input_text)
    assert "engine_tools" not in payload
    discovery = payload["discovery"]
    assert discovery["total"] == len(catalog)
    assert discovery["tier"] != TIER_EAGER
    blob = json.dumps(payload, ensure_ascii=False)
    assert blob.count("send a short note") < 8
    assert "properties" not in json.dumps(discovery.get("listing") or [])


def test_plan_loop_search_then_describe_then_ok() -> None:
    def script(calls: int, request: ModelRequest) -> str:
        payload = json.loads(request.input_text)
        if calls == 1:
            assert "discovery" in payload
            assert payload.get("lookups") == []
            return json.dumps(
                {"ok": False, "lookup": {"op": "search", "query": "weather"}}
            )
        if calls == 2:
            lookups = payload["lookups"]
            assert lookups[0]["op"] == "search"
            hits = [item["name"] for item in lookups[0]["hits"]]
            assert "get_weather" in hits
            return json.dumps(
                {"ok": False, "lookup": {"op": "describe", "name": "get_weather"}}
            )
        described = payload["lookups"][1]
        assert described["op"] == "describe"
        assert described["tools"][0]["name"] == "get_weather"
        return json.dumps(
            {
                "ok": True,
                "template": "generic",
                "command": "get_weather",
                "params": {"city": "杭州"},
            }
        )

    port = _JsonPort(script)
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=_big_catalog(),
    )
    planned = planner.plan(_intake())
    assert isinstance(planned, ToolRequest)
    assert planned.command == "get_weather"
    assert planned.params["city"] == "杭州"
    assert port.calls == 3


def test_plan_loop_exceeds_lookup_rounds() -> None:
    def script(calls: int, _request: ModelRequest) -> str:
        del calls
        return json.dumps({"ok": False, "lookup": {"op": "search", "query": "weather"}})

    port = _JsonPort(script)
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=_big_catalog(),
    )
    planned = planner.plan(_intake())
    assert isinstance(planned, PlanFailure)
    assert "我这边" in planned.message
    assert "lookup" not in planned.message
    assert port.calls == LOOKUP_MAX_ROUNDS + 1


def test_instruction_says_not_to_use_old_scene_failures() -> None:
    text = ToolPlanSkill.instruction
    assert "片场里旧的失败" in text
    assert "lookup.search" in text
    assert "engine_tools" in text


def test_unknown_command_still_rejected_on_large_catalog() -> None:
    port = _JsonPort(
        {"ok": True, "template": "generic", "command": "not-a-command"}
    )
    planner = SkillPlanner(
        ToolPlanSkill(port),
        catalog=load_catalog(),
        engine_tools=_big_catalog(),
    )
    planned = planner.plan(_intake())
    assert isinstance(planned, PlanFailure)
    assert "引擎目录" in planned.message
