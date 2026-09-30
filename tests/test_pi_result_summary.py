"""Pi 整轮 RESULT：探路 bash 不得占满摘要。"""

import json

from jshi.tool.contract import ToolRequest
from jshi.tool.pi_engine import (
    PiEngine,
    _compact_shell_summary,
    _join_shell_result_parts,
    _shell_output_is_exploration,
)


def test_skill_dir_listing_is_exploration() -> None:
    text = (
        r"C:\Users\40575\Desktop\prog\main\.tmp\pi\agent\skills\exchange-rate\scripts:"
        "\nexchange-rate.ps1\nexchange-rate.sh\n\n"
        r"C:\Users\40575\Desktop\prog\main\.tmp\pi\agent\skills\web-search:"
        "\nSKILL.md\nscripts\ntool.json"
    )
    assert _shell_output_is_exploration(text) is True


def test_skill_md_read_is_exploration() -> None:
    text = """---
name: web-search
description: 联网搜索
---

# web-search

能力意图：联网搜索
参数格式：{"type": "object"}
"""
    assert _shell_output_is_exploration(text) is True


def test_rate_and_search_json_are_kept() -> None:
    rate = (
        "base=CNY quote=USD rate=0.148908 "
        "updated_at=Mon, 14 Sep 2026 00:02:31 +0000 "
        "source=open.er-api.com"
    )
    search = (
        '{"query": "果蝇 大脑 连接组 图谱", "engine": "bing", '
        '"count": 8, "results": [{"title": "果蝇"}]}'
    )
    assert _shell_output_is_exploration(rate) is False
    assert _shell_output_is_exploration(search) is False


def test_python_probe_output_is_exploration() -> None:
    assert _shell_output_is_exploration("query_news.py\nPython 3.14.7") is True


def test_news_json_drops_probe_and_exit_marker() -> None:
    body = '{"count":10,"articles":[]}'
    assert _join_shell_result_parts([
        "query_news.py\nPython 3.14.7",
        body + "\nEXIT=0",
    ]) == body


def test_skill_prompt_passes_structured_params() -> None:
    engine = PiEngine()
    prompt = engine._build_prompt(ToolRequest(
        need="换备用源查新闻",
        command="skill:news-top-stories-query",
        params={"date": "2026-09-29", "limit": 10, "source_preference": "npr"},
    ))
    assert prompt.startswith("/skill:news-top-stories-query 换备用源查新闻")
    assert '"source_preference":"npr"' in prompt


def test_join_drops_exploration_so_short_summary_is_useful() -> None:
    listing = (
        r"C:\tmp\.tmp\pi\agent\skills\exchange-rate\scripts:"
        "\nexchange-rate.ps1\nexchange-rate.sh"
    )
    rate = "base=CNY quote=USD rate=0.148908 source=open.er-api.com"
    joined = _join_shell_result_parts([listing, rate])
    assert "exchange-rate.ps1" not in joined
    assert "rate=0.148908" in joined
    assert joined[:200].startswith("base=CNY")


def test_compact_summary_keeps_rate_when_search_json_is_long() -> None:
    listing = (
        r"C:\tmp\.tmp\pi\agent\skills\exchange-rate\scripts:"
        "\nexchange-rate.ps1\nexchange-rate.sh"
    )
    search = (
        '{"query": "果蝇大脑 连接组 最新消息", "engine": "bing", '
        '"count": 8, "results": [{"title": "果蝇（果蝇科昆虫的通称）_百度百科", '
        '"snippet": "' + ("通称介绍。" * 40) + '"}]}'
    )
    rate = (
        "base=CNY quote=USD rate=0.148908 "
        "updated_at=Mon, 14 Sep 2026 00:02:31 +0000 "
        "source=open.er-api.com"
    )
    short = _compact_shell_summary([listing, search, rate], limit=1500)
    assert "rate=0.148908" in short
    assert "exchange-rate.ps1" not in short


def test_shell_exploration_does_not_emit_progress(tmp_path) -> None:
    engine = PiEngine(home_dir=tmp_path)
    request = ToolRequest(need="查天气", command="skill:weather")
    listing = r"C:\tmp\agent\skills\weather\scripts:" + "\nweather.ps1\nSKILL.md"
    event = {
        "type": "tool_execution_update",
        "toolName": "bash",
        "partialResult": {"content": [{"type": "text", "text": listing}]},
    }
    assert engine._convert_event(request, event) is None
    event["partialResult"]["content"][0]["text"] = "base=CNY rate=0.148908"
    progress = engine._convert_event(request, event)
    assert progress is not None
    assert progress.progress is not None
    assert "0.148908" in progress.progress.partial


def test_created_skill_requires_files_and_matching_metadata(tmp_path) -> None:
    engine = PiEngine(home_dir=tmp_path)
    assert engine._created_skill_error("weather")
    folder = tmp_path / "agent" / "skills" / "weather"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: weather\n---\n", encoding="utf-8")
    (folder / "tool.json").write_text(
        json.dumps({"tool_name": "weather"}), encoding="utf-8"
    )
    assert engine._created_skill_error("weather") == ""
