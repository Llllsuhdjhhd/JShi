"""片场压缩：mod 不得把具体事实压成「都还在」的空话。"""

from __future__ import annotations

from jshi.style.packs import EDIT_RULES, ZONE_WRITE_RULES
from jshi.style.zone import ZoneStore, block_facts, hollow_mod

NEWS = (
    "我说：“今天 NPR 的十条新闻："
    "1.《美联储维持利率不变》https://npr.org/a1；"
    "2.《加州山火蔓延》https://npr.org/a2；"
    "3.《火星样本返回推迟》https://npr.org/a3；"
    "4.《国会通过预算案》https://npr.org/a4”"
)


def _zone_with(*blocks: str) -> ZoneStore:
    zone = ZoneStore()
    zone.save("s", blocks)
    return zone


def test_block_facts_skip_outer_reply_quote():
    facts = block_facts(NEWS)
    assert "美联储维持利率不变" in facts
    assert "npr" in facts
    assert not any(fact.startswith("今天 npr") for fact in facts)


def test_hollow_claim_turns_mod_into_del():
    zone = _zone_with("lux说：“帮我看看今天的新闻”", NEWS)
    result = zone.apply_edit(
        "s",
        [{"op": "mod", "id": "B2", "text": "十条新闻已经取到，来源是 NPR，链接还在"}],
    )
    assert result == ("lux说：“帮我看看今天的新闻”",)
    assert zone.last_hollow_mods[0][0] == "B2"


def test_mod_that_drops_most_facts_is_deleted():
    assert hollow_mod(NEWS, "我给 lux 念了几条新闻")


def test_trimming_pleasantries_keeps_mod():
    old = "我说：“哎呀，好的好的，那个嘛，明天 10 点在西湖边的《断桥》茶馆见，记得带 3 本书哦”"
    new = "我说：“明天 10 点在西湖边的《断桥》茶馆见，带 3 本书”"
    zone = _zone_with(old)
    assert zone.apply_edit("s", [{"op": "mod", "id": "B1", "text": new}]) == (new,)
    assert zone.last_hollow_mods == ()


def test_partial_list_with_originals_is_allowed():
    new = (
        "我说：“十条里只留了这两条：1.《美联储维持利率不变》https://npr.org/a1；"
        "2.《加州山火蔓延》https://npr.org/a2”"
    )
    assert not hollow_mod(NEWS, new)


def test_few_fact_block_mod_is_not_checked():
    assert not hollow_mod("lux说她今天有点累，想早点休息", "lux累了")


def test_prompt_states_compression_order():
    assert "【压缩片场】" in ZONE_WRITE_RULES
    assert "整块 del" in ZONE_WRITE_RULES
    assert "链接还在" in ZONE_WRITE_RULES
    assert "按 del 处理" in EDIT_RULES
