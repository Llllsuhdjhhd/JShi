"""魔法书：窗口 100 万，活跃区 1/30，工作集非保护 1500 字，价值 活跃区×1/5。"""

from jshi.core.params import (
    ACTIVE_ZONE_CHARS,
    DEFAULT_MODEL_CONTEXT_WINDOW,
    WORKING_SET_LIMIT_CHARS,
    active_zone_chars,
    value_load_char_budget,
    working_set_limit,
)


def test_budget_stack_for_one_million_window():
    assert DEFAULT_MODEL_CONTEXT_WINDOW == 1_000_000
    assert active_zone_chars() == 33_333
    assert ACTIVE_ZONE_CHARS == 33_333
    assert WORKING_SET_LIMIT_CHARS == 1500
    assert working_set_limit() == 1500
    assert value_load_char_budget() == 6_667
