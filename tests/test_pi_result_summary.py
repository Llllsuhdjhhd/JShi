"""Pi 整轮 RESULT：探路 bash 不得占满摘要。"""

from jshi.tool.pi_engine import (
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
