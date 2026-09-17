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

# 03 工作集：非保护片段（本轮回忆 + 普通价值）的字符预算。
# 身份 / 对象 / 既往视图正文 / 08 常驻不占此预算。固定字数，不随窗口放大。
WORKING_SET_LIMIT_CHARS: int = 1500

# 普通价值装载占活跃区字符预算的比例。按字截断；binding 边界不占此预算。
VALUE_LOAD_RATIO: float = 1 / 5

# 苏西坡 / 斯密斯（人格）片场预算：固定约 2500 字，独立于木头活跃区。
# 2026-09-12 由 2000 提到 2500：块要带时间标注后，同一块数占的字变多。
SUXIPO_ZONE_CHARS: int = 2500

# 人格「价值叙述」字数上限：boot 时单独生成、独立于片场预算。
VALUE_NARRATION_CHARS: int = 300


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
    """03 工作集非保护片段的字符预算，默认 1500 字。

    不随模型窗口放大。``context_window`` 保留签名，当前忽略。
    """
    return max(1, int(WORKING_SET_LIMIT_CHARS))


def suxipo_zone_chars(context_window: int | None = None) -> int:
    """人格片场字符上限：固定约 2500 字，独立于木头活跃区。"""
    del context_window
    return max(1, int(SUXIPO_ZONE_CHARS))


def value_narration_chars(context_window: int | None = None) -> int:
    """人格「价值叙述」字数上限：boot 时单独生成，独立于片场预算。"""
    del context_window
    return max(1, int(VALUE_NARRATION_CHARS))


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


# ---------------------------------------------------------------------------- #
# 300 自省（全部占位，由 400 调参并留审计；见《doc/design/300-自省.md》/《方案 300》）
# ---------------------------------------------------------------------------- #

# 总开关：关掉后不入队、不起作用（回退到 placeholder 行为）
INTROSPECTION_ENABLED: bool = True

# 现场时间窗 Δ：条目前后各取多久（按时间取，不按段数）
INTROSPECTION_SCENE_WINDOW_SECONDS: int = 600
# 现场段数上限（超了留离时间中心最近的）
INTROSPECTION_SCENE_MAX_SEGMENTS: int = 40
# 09 回溯召回条数
INTROSPECTION_RECALL_LIMIT: int = 6
# 同一主题的冷却（秒）
INTROSPECTION_COOLDOWN_SECONDS: int = 3600
# 队列上限（满了丢最"缓"的一条）
INTROSPECTION_MAX_QUEUE: int = 8
# 单次自省超时（本刀未接入线程，仅登记；超时执行留到 daemon worker 一刀）
INTROSPECTION_TIMEOUT_SECONDS: int = 90
# 各级模型调用次数（低档 0 次 = 只落规则摘要）
INTROSPECTION_MODEL_CALLS_LOW: int = 0
INTROSPECTION_MODEL_CALLS_MEDIUM: int = 1
INTROSPECTION_MODEL_CALLS_HIGH: int = 3
# 闲时回顾只在"最近 K 段"里挑，避免从最老开始翻旧账
INTROSPECTION_IDLE_WINDOW: int = 40

__all__ = [
    "ACTIVE_ZONE_CHARS",
    "ACTIVE_ZONE_RATIO",
    "DEFAULT_MODEL_CONTEXT_WINDOW",
    "INTROSPECTION_COOLDOWN_SECONDS",
    "INTROSPECTION_ENABLED",
    "INTROSPECTION_IDLE_WINDOW",
    "INTROSPECTION_MAX_QUEUE",
    "INTROSPECTION_MODEL_CALLS_HIGH",
    "INTROSPECTION_MODEL_CALLS_LOW",
    "INTROSPECTION_MODEL_CALLS_MEDIUM",
    "INTROSPECTION_RECALL_LIMIT",
    "INTROSPECTION_SCENE_MAX_SEGMENTS",
    "INTROSPECTION_SCENE_WINDOW_SECONDS",
    "INTROSPECTION_TIMEOUT_SECONDS",
    "SUXIPO_ZONE_CHARS",
    "VALUE_CATALOG_REFRESH_SECONDS",
    "VALUE_LOAD_RATIO",
    "VALUE_NARRATION_CHARS",
    "WORKING_SET_LIMIT_CHARS",
    "active_zone_chars",
    "suxipo_zone_chars",
    "value_narration_chars",
    "value_load_char_budget",
    "working_set_limit",
]
