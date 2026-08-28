"""魔法书：集中登记"魔法数"（影响行为的实验参数）。

为什么要集中：目前数值散落在各模块默认值里（对象置信度档位、个人世界额度、
活跃区字符上限、记忆冲刷参数、工作集上限等），既难找到、也容易各自漂移、
无法被 14（统计分析与参数调优）统一读取与调参。集中到这里之后：

- 每个数值给出默认值与注释；
- 由 14 统一读取、调参，并留审计（I-002 / I-005）；
- 调整只影响实验基线，不改变稳定原则（00 第 8 节）。

当前进度：03 工作集上限已接入本模块（见 ``working_set_limit``）。
其余数值暂仍留在原模块，作为迁移目标列在下方"待迁移"；迁移时应同步更新
各模块默认值与对应测试。

约定：只登记会影响行为、需要被调参/统计/审计的数值；稳定原则不要动。
汉字粗算 1 字 ≈ 1 token。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------- #
# 上下文与工作集（基线按 100 万 token 窗口）
# ---------------------------------------------------------------------------- #

# 匠石心智所用模型的上下文窗口。应读自实际模型/配置（由 14 接管）。
DEFAULT_MODEL_CONTEXT_WINDOW: int = 1_000_000

# 活跃区占窗口的比例。见《概念词汇》「活跃区比」。
ACTIVE_ZONE_RATIO: float = 1 / 30

# 03 工作集：非保护片段的字符预算占窗口的比例。
# 只裁 always=False 的非保护片段；身份 / 对象 / 既往视图正文 / 08 常驻不占此预算。
WORKING_SET_LIMIT_RATIO: float = 1 / 15

# 普通价值装载占活跃区字符预算的比例。按字截断；binding 边界不占此预算。
VALUE_LOAD_RATIO: float = 1 / 5


def _base_window(context_window: int | None = None) -> int:
    return (
        context_window
        if context_window is not None
        else DEFAULT_MODEL_CONTEXT_WINDOW
    )


def active_zone_chars(context_window: int | None = None) -> int:
    """16 活跃区字符硬上限：窗口 × 1/30，至少 1。"""
    return max(1, round(_base_window(context_window) * ACTIVE_ZONE_RATIO))


def working_set_limit(context_window: int | None = None) -> int:
    """03 工作集非保护片段的字符预算：窗口 × 1/15，至少 1。

    ``context_window`` 为模型上下文窗口（token）；缺省用
    ``DEFAULT_MODEL_CONTEXT_WINDOW``。返回字符预算，不是片段条数。
    """
    return max(1, round(_base_window(context_window) * WORKING_SET_LIMIT_RATIO))


def value_load_char_budget(active_zone: int | None = None) -> int:
    """普通价值装载的字符预算：活跃区上限 × 1/5，至少 1。"""
    cap = active_zone_chars() if active_zone is None else int(active_zone)
    return max(1, round(cap * VALUE_LOAD_RATIO))


# 与 experienceledger.active_window_chars 对齐的默认值（随窗口与比例计算）。
ACTIVE_ZONE_CHARS: int = active_zone_chars()


# ---------------------------------------------------------------------------- #
# 待迁移（当前仍定义在原模块，集中为"魔法书"目标值）
# ---------------------------------------------------------------------------- #
# - 对象识别置信度档位        -> src/jshi/recognition/port.py (CONF_* / bound_/carrier_/name_/memory_confidence)
# - 个人世界额度 LOAD_QUOTAS   -> src/jshi/personalworld/port.py（普通价值已改按字，此表只管其余种类）
# - 记忆冲刷参数 flush_*       -> src/jshi/memorycontrol/inprocess.py
# 迁移时：在各自模块 import 本模块常量，勿再写死默认值。

# 100 普通价值何时重新从目录取一份快照，写入工作集。目录变更（导入/审核/加锁/废止）
# 立即触发；否则按间隔。0 表示只按目录变更，不按时间。
VALUE_CATALOG_REFRESH_SECONDS: float = 86400.0

__all__ = [
    "ACTIVE_ZONE_CHARS",
    "ACTIVE_ZONE_RATIO",
    "DEFAULT_MODEL_CONTEXT_WINDOW",
    "VALUE_CATALOG_REFRESH_SECONDS",
    "VALUE_LOAD_RATIO",
    "WORKING_SET_LIMIT_RATIO",
    "active_zone_chars",
    "value_load_char_budget",
    "working_set_limit",
]
