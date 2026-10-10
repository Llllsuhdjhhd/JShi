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

from dataclasses import dataclass

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

# 苏西坡 / 斯密斯（人格）片场预算：约 5000 字，独立于木头活跃区。
# 取记忆投递阈值（3000 字）的 1.5–2 倍，让尚未投递的原话仍能留在片场里。
SUXIPO_ZONE_CHARS: int = 5000

# 人格「价值叙述」字数上限：boot 时单独生成、独立于片场预算。
VALUE_NARRATION_CHARS: int = 300

# 09 记忆按量投递：尚未存入记忆的原话累计到这个字数就投递。
MEMORY_FLUSH_CHARS: int = 3000


def _current(name: str, default: int | float | bool) -> int | float | bool:
    """读进程里的当前快照。未安装时用登记的默认值。"""
    from jshi.core.param_overlay import current_value

    return current_value(name, default)


def _base_window(context_window: int | None = None) -> int:
    if context_window is not None:
        return int(context_window)
    return int(_current("DEFAULT_MODEL_CONTEXT_WINDOW", DEFAULT_MODEL_CONTEXT_WINDOW))


def active_zone_chars(context_window: int | None = None) -> int:
    """16 活跃区字符硬上限：窗口 × 活跃区比例，至少 1。"""
    ratio = float(_current("ACTIVE_ZONE_RATIO", ACTIVE_ZONE_RATIO))
    return max(1, round(_base_window(context_window) * ratio))


def working_set_limit(context_window: int | None = None) -> int:
    """03 工作集非保护片段的字符预算，默认 1500 字。

    不随模型窗口放大。``context_window`` 保留签名，当前忽略。
    """
    del context_window
    return max(1, int(_current("WORKING_SET_LIMIT_CHARS", WORKING_SET_LIMIT_CHARS)))


def suxipo_zone_chars(context_window: int | None = None) -> int:
    """人格片场字符上限：约 5000 字，约为记忆投递阈值的 1.5–2 倍。"""
    del context_window
    return max(1, int(_current("SUXIPO_ZONE_CHARS", SUXIPO_ZONE_CHARS)))


def value_narration_chars(context_window: int | None = None) -> int:
    """人格「价值叙述」字数上限：boot 时单独生成，独立于片场预算。"""
    del context_window
    return max(1, int(_current("VALUE_NARRATION_CHARS", VALUE_NARRATION_CHARS)))


def memory_flush_chars() -> int:
    """09 按量投递的字数阈值，默认 3000 字。"""
    return max(1, int(_current("MEMORY_FLUSH_CHARS", MEMORY_FLUSH_CHARS)))


def value_catalog_refresh_seconds() -> float:
    return float(
        _current("VALUE_CATALOG_REFRESH_SECONDS", VALUE_CATALOG_REFRESH_SECONDS)
    )


def value_load_char_budget(active_zone: int | None = None) -> int:
    """普通价值装载的字符预算：活跃区上限 × 比例，至少 1。"""
    cap = active_zone_chars() if active_zone is None else int(active_zone)
    ratio = float(_current("VALUE_LOAD_RATIO", VALUE_LOAD_RATIO))
    return max(1, round(cap * ratio))


def introspection_enabled() -> bool:
    return bool(_current("INTROSPECTION_ENABLED", INTROSPECTION_ENABLED))


def introspection_scene_window_seconds() -> int:
    return int(
        _current(
            "INTROSPECTION_SCENE_WINDOW_SECONDS",
            INTROSPECTION_SCENE_WINDOW_SECONDS,
        )
    )


def introspection_scene_max_segments() -> int:
    return int(
        _current("INTROSPECTION_SCENE_MAX_SEGMENTS", INTROSPECTION_SCENE_MAX_SEGMENTS)
    )


def introspection_recall_limit() -> int:
    return int(_current("INTROSPECTION_RECALL_LIMIT", INTROSPECTION_RECALL_LIMIT))


def introspection_cooldown_seconds() -> int:
    return int(
        _current("INTROSPECTION_COOLDOWN_SECONDS", INTROSPECTION_COOLDOWN_SECONDS)
    )


def introspection_max_queue() -> int:
    return int(_current("INTROSPECTION_MAX_QUEUE", INTROSPECTION_MAX_QUEUE))


def introspection_idle_window() -> int:
    return int(_current("INTROSPECTION_IDLE_WINDOW", INTROSPECTION_IDLE_WINDOW))


def step_inputs_max_bytes() -> int:
    return int(_current("STEP_INPUTS_MAX_BYTES", STEP_INPUTS_MAX_BYTES))


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

# 重放输入文件的字节上限。超出后删最旧的调用。本阶段 locked，不参与调参。
STEP_INPUTS_MAX_BYTES: int = 2_097_152

# 片场上限不低于投递阈值的这个倍数。来源见 ParamConstraint 说明。
ZONE_FLUSH_RATIO: float = 1.5


@dataclass(frozen=True)
class ParamSpec:
    """一项参数的登记。默认值仍是上面的模块常量。"""

    name: str
    default: int | float | bool | None
    low: int | float | None
    high: int | float | None
    level: str  # auto | review | locked
    metrics: tuple[str, ...] = ()
    store: str = "overlay"  # overlay | recall_strategy
    kind: str = "int"  # int | float | bool


@dataclass(frozen=True)
class ParamConstraint:
    """片场上限应不小于投递阈值的 1.5 倍。

    1.5 取自片场预算注释里的下限：尚未投递的原话还要留在片场里。
    人格生效还要求素材达到片场预算的一半（``subject/process.py``）。
    不取注释里的上限 2。默认 5000 只比 3000 的 1.5 倍（4500）多 500。
    """

    name: str
    left: str
    right: str
    ratio: float


REGISTRY: dict[str, ParamSpec] = {
    spec.name: spec
    for spec in (
        ParamSpec(
            "DEFAULT_MODEL_CONTEXT_WINDOW",
            DEFAULT_MODEL_CONTEXT_WINDOW,
            100_000,
            2_000_000,
            "locked",
            kind="int",
        ),
        ParamSpec(
            "ACTIVE_ZONE_RATIO",
            ACTIVE_ZONE_RATIO,
            1 / 60,
            1 / 5,
            "review",
            ("反应时间",),
            kind="float",
        ),
        ParamSpec(
            "WORKING_SET_LIMIT_CHARS",
            WORKING_SET_LIMIT_CHARS,
            200,
            8_000,
            "review",
            ("回忆质量", "费用"),
            kind="int",
        ),
        ParamSpec(
            "VALUE_LOAD_RATIO",
            VALUE_LOAD_RATIO,
            0.05,
            0.5,
            "review",
            kind="float",
        ),
        ParamSpec(
            "SUXIPO_ZONE_CHARS",
            SUXIPO_ZONE_CHARS,
            1_500,
            20_000,
            "review",
            ("费用", "记忆质量", "反应时间"),
            kind="int",
        ),
        ParamSpec(
            "VALUE_NARRATION_CHARS",
            VALUE_NARRATION_CHARS,
            50,
            2_000,
            "locked",
            kind="int",
        ),
        ParamSpec(
            "MEMORY_FLUSH_CHARS",
            MEMORY_FLUSH_CHARS,
            1_000,
            8_000,
            "review",
            ("费用", "记忆质量", "反应时间"),
            kind="int",
        ),
        ParamSpec(
            "VALUE_CATALOG_REFRESH_SECONDS",
            VALUE_CATALOG_REFRESH_SECONDS,
            0,
            604_800,
            "review",
            kind="float",
        ),
        ParamSpec(
            "INTROSPECTION_ENABLED",
            INTROSPECTION_ENABLED,
            None,
            None,
            "locked",
            kind="bool",
        ),
        ParamSpec(
            "INTROSPECTION_SCENE_WINDOW_SECONDS",
            INTROSPECTION_SCENE_WINDOW_SECONDS,
            60,
            3_600,
            "review",
            kind="int",
        ),
        ParamSpec(
            "INTROSPECTION_SCENE_MAX_SEGMENTS",
            INTROSPECTION_SCENE_MAX_SEGMENTS,
            1,
            200,
            "review",
            kind="int",
        ),
        ParamSpec(
            "INTROSPECTION_RECALL_LIMIT",
            INTROSPECTION_RECALL_LIMIT,
            0,
            30,
            "review",
            kind="int",
        ),
        ParamSpec(
            "INTROSPECTION_COOLDOWN_SECONDS",
            INTROSPECTION_COOLDOWN_SECONDS,
            0,
            86_400,
            "review",
            kind="int",
        ),
        ParamSpec(
            "INTROSPECTION_MAX_QUEUE",
            INTROSPECTION_MAX_QUEUE,
            1,
            32,
            "review",
            kind="int",
        ),
        ParamSpec(
            "INTROSPECTION_TIMEOUT_SECONDS",
            INTROSPECTION_TIMEOUT_SECONDS,
            1,
            600,
            "review",
            kind="int",
        ),
        ParamSpec(
            "INTROSPECTION_MODEL_CALLS_LOW",
            INTROSPECTION_MODEL_CALLS_LOW,
            0,
            5,
            "review",
            kind="int",
        ),
        ParamSpec(
            "INTROSPECTION_MODEL_CALLS_MEDIUM",
            INTROSPECTION_MODEL_CALLS_MEDIUM,
            0,
            5,
            "review",
            kind="int",
        ),
        ParamSpec(
            "INTROSPECTION_MODEL_CALLS_HIGH",
            INTROSPECTION_MODEL_CALLS_HIGH,
            0,
            10,
            "review",
            kind="int",
        ),
        ParamSpec(
            "INTROSPECTION_IDLE_WINDOW",
            INTROSPECTION_IDLE_WINDOW,
            1,
            500,
            "review",
            kind="int",
        ),
        ParamSpec(
            "STEP_INPUTS_MAX_BYTES",
            STEP_INPUTS_MAX_BYTES,
            1_024,
            50_000_000,
            "locked",
            kind="int",
        ),
        ParamSpec(
            "recall.default_level",
            1,
            1,
            9,
            "auto",
            ("回忆质量",),
            store="recall_strategy",
            kind="int",
        ),
        ParamSpec(
            "recall.limit",
            None,
            0,
            64,
            "auto",
            ("回忆质量", "费用"),
            store="recall_strategy",
            kind="int",
        ),
    )
}

ZONE_FLUSH_CONSTRAINT = ParamConstraint(
    name="zone_at_least_1_5_flush",
    left="SUXIPO_ZONE_CHARS",
    right="MEMORY_FLUSH_CHARS",
    ratio=ZONE_FLUSH_RATIO,
)

# 回忆自省：容量按 UTF-8 正文字节计，不含 SQLite 索引/页开销。
REFLECTION_DEFAULTS = {
    "REFLECTION_INTERVAL_SECONDS": (600, 1, 86400, "int"),
    "REFLECTION_IDLE_SECONDS": (30, 0, 3600, "int"),
    "REFLECTION_POLL_SECONDS": (2, 0.1, 60, "float"),
    "REFLECTION_PRESSURE_BYTES": (1048576, 256, 20971520, "int"),
    "REFLECTION_RENEW_RATIO": (1 / 3, 0.01, 1, "float"),
    "REFLECTION_COMPARE_ITEMS": (24, 4, 100, "int"),
    "REFLECTION_ITEM_CHARS": (2000, 100, 10000, "int"),
    "REFLECTION_CONTEXT_CHARS": (24000, 1000, 100000, "int"),
    "REFLECTION_MEMORY_BYTES": (20971520, 256, 209715200, "int"),
    "REFLECTION_MEMORY_RATIO": (0.1, 0.01, 0.5, "float"),
    "REFLECTION_MEMORY_BOOTSTRAP_BYTES": (65536, 256, 1048576, "int"),
    "REFLECTION_RECALL_RATIO": (0.2, 0.01, 0.5, "float"),
    "REFLECTION_FEEDBACK_WEIGHT": (0.35, 0, 0.5, "float"),
    "REFLECTION_FEEDBACK_SIMILARITY": (0.3, 0.01, 1, "float"),
    "REFLECTION_FEEDBACK_DAYS": (30, 1, 365, "int"),
    "REFLECTION_FEEDBACK_ROWS": (500, 1, 10000, "int"),
    "REFLECTION_CANDIDATE_MULTIPLIER": (4, 1, 10, "int"),
    "REFLECTION_OBSERVATIONS": (100, 1, 1000, "int"),
    "REFLECTION_DB_TIMEOUT_SECONDS": (0.1, 0.01, 1, "float"),
    "REFLECTION_INSIGHT_CANDIDATES": (100, 1, 1000, "int"),
    "REFLECTION_MEMORY_HALF_LIFE_DAYS": (60, 1, 3650, "int"),
    "REFLECTION_STANDARD_CHARS": (2000, 100, 10000, "int"),
}
for _name, (_default, _low, _high, _kind) in REFLECTION_DEFAULTS.items():
    REGISTRY[_name] = ParamSpec(_name, _default, _low, _high, "review",
                              ("回忆质量", "自省", "费用"), kind=_kind)


def reflection_param(name: str) -> int | float:
    """新自省模块仍使用魔法书及同一覆盖快照。"""
    return _current(name, REFLECTION_DEFAULTS[name][0])


def lookup(name: str) -> ParamSpec:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise KeyError(name) from exc


__all__ = [
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
    "MEMORY_FLUSH_CHARS",
    "ParamConstraint",
    "ParamSpec",
    "REGISTRY",
    "STEP_INPUTS_MAX_BYTES",
    "SUXIPO_ZONE_CHARS",
    "VALUE_CATALOG_REFRESH_SECONDS",
    "VALUE_LOAD_RATIO",
    "VALUE_NARRATION_CHARS",
    "WORKING_SET_LIMIT_CHARS",
    "ZONE_FLUSH_CONSTRAINT",
    "ZONE_FLUSH_RATIO",
    "active_zone_chars",
    "introspection_cooldown_seconds",
    "introspection_enabled",
    "introspection_idle_window",
    "introspection_max_queue",
    "introspection_recall_limit",
    "introspection_scene_max_segments",
    "introspection_scene_window_seconds",
    "lookup",
    "memory_flush_chars",
    "step_inputs_max_bytes",
    "suxipo_zone_chars",
    "value_catalog_refresh_seconds",
    "value_load_char_budget",
    "value_narration_chars",
    "working_set_limit",
]
