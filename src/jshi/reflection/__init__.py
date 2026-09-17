"""300 自省：无直接外部输入时的内在活动。

对齐《doc/design/300-自省.md》：

- 触发 → 统一信封 → 判定（气口）→ 入队 → 现场 → 六问 → 落记录；
- 只接已发生、已有记录的刺激；与主流程并行，不挡说话；
- 产出只到候选；不写 08 / 100，不调 ``append_internal``。

``PlaceholderReflection`` 保留作回退（旧的最小反思逻辑）。

导入约定：本包**只急切导入 ``port``**；引擎 / 队列 / 现场 / 六问用 ``__getattr__`` 懒加载，
避免 ``jshi.skill → jshi.reflection.port → jshi.reflection.__init__ → inprocess → jshi.skill`` 的环。
"""

from __future__ import annotations

from typing import Any

from .port import (
    MOMENT_AFTER_ACTIVITY,
    MOMENT_EXPLICIT,
    MOMENT_IDLE,
    NOT_EVALUATED,
    NO_MATERIAL,
    IntrospectionAnswer,
    IntrospectionLevel,
    IntrospectionPort,
    IntrospectionRequest,
    IntrospectionRun,
    IntrospectionScene,
    IntrospectionSource,
    IntrospectionStatus,
    IntrospectionTrigger,
    IntrospectionUrgency,
    PlaceholderReflection,
    ReflectionPort,
    SourceContext,
    level_rank,
    urgency_rank,
    utc_now,
)

_LAZY: dict[str, tuple[str, str]] = {
    "InProcessReflection": (".inprocess", "InProcessReflection"),
    "EnqueueResult": (".queue", "EnqueueResult"),
    "IntrospectionQueue": (".queue", "IntrospectionQueue"),
    "build_scene": (".scene", "build_scene"),
    "render_scene": (".scene", "render_scene"),
    "segment_text": (".scene", "segment_text"),
    "run_introspection": (".runner", "run_introspection"),
    "default_sources": (".sources", "default_sources"),
    "detect": (".sources", "detect"),
    "merge": (".sources", "merge"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value
    return value


__all__ = [
    "IntrospectionAnswer",
    "IntrospectionLevel",
    "IntrospectionPort",
    "IntrospectionRequest",
    "IntrospectionRun",
    "IntrospectionScene",
    "IntrospectionSource",
    "IntrospectionStatus",
    "IntrospectionTrigger",
    "IntrospectionUrgency",
    "MOMENT_AFTER_ACTIVITY",
    "MOMENT_EXPLICIT",
    "MOMENT_IDLE",
    "NOT_EVALUATED",
    "NO_MATERIAL",
    "PlaceholderReflection",
    "ReflectionPort",
    "SourceContext",
    "level_rank",
    "urgency_rank",
    "utc_now",
    *_LAZY,
]
