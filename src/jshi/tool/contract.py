"""工具使用契约：调用方（05 / 自省）与工具使用模块之间的数据结构。

对齐《doc/design/工具使用.md》：
- 反馈覆盖工具使用全流程：使用前（estimate）、使用中（progress）、使用后（result）。
- 字段只增不改 + ``version`` + 开放 ``meta``：方便日后演进，不破坏旧消费方。
- 工具目录 / 包管理归引擎（如 Pi），不在匠石侧自建注册表。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


def normalize_tool_name(raw: str) -> str:
    """把模型给的造工具名规范化成引擎可用的技能名（小写、数字、连字符）。

    规范只能做一次，而且要在**落账之前**：否则账本记 ``weather_forecast``、
    Pi 那边造出 ``weather-forecast``、实测统计又按账本的名字分组，三处对不上。
    """
    value = (raw or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:64] or "generated-tool"


class ToolOrigin(StrEnum):
    """哪一面触发这次工具使用。"""

    EXTERNAL_05 = "external_05"  # 外部认知（05 主流程）
    INTROSPECTION = "introspection"  # 自省（内部活动）


class AskMode(StrEnum):
    """调用方希望怎么走：先报价再决定、直接执行，或先造一个工具。"""

    PROPOSE_ONLY = "propose_only"
    EXECUTE = "execute"
    CREATE_TOOL = "create_tool"


class FeedbackKind(StrEnum):
    """过程反馈类型。开放枚举：后续可加 interrupt / verify / retry 等。"""

    ESTIMATE = "estimate"
    PROGRESS = "progress"
    RESULT = "result"


class ToolStatus(StrEnum):
    """结果状态。"""

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    REJECTED = "rejected"
    ABORTED = "aborted"


@dataclass(frozen=True)
class Budget:
    """调用方设定的预算：成本 / 时间 / 资源上限（软约束，由模板语义解释）。"""

    cost_cap: float | None = None
    timeout_ms: int | None = None
    max_steps: int | None = None
    max_tools: int | None = None
    max_tokens: int | None = None
    resource_caps: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PermissionContext:
    """权限 / 边界上下文（对接 100 / 15 / 11）。"""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    require_confirm: bool = False
    note: str = ""


# 预算暂设到最大。真正的预算模块与真源都还没建（见《工具使用》§8.6），
# 现在只把「上限」透给模型，让它在判定「能不能造」时有依据可读。
# 各项为 None 表示不设上限，也就是暂时一切都够。
MAX_BUDGET = Budget()


def budget_to_dict(budget: Budget) -> dict[str, Any]:
    """摊成能进任务 JSON 的普通字典。``None`` 表示不设上限。

    另给一个 ``unlimited`` 布尔，免得模型把「全 None」误读成「没给预算」。
    """
    caps = (
        budget.cost_cap,
        budget.timeout_ms,
        budget.max_steps,
        budget.max_tools,
        budget.max_tokens,
    )
    return {
        "cost_cap": budget.cost_cap,
        "timeout_ms": budget.timeout_ms,
        "max_steps": budget.max_steps,
        "max_tools": budget.max_tools,
        "max_tokens": budget.max_tokens,
        "resource_caps": dict(budget.resource_caps),
        "unlimited": all(value is None for value in caps) and not budget.resource_caps,
    }


@dataclass(frozen=True)
class CreateToolSpec:
    """205 判明「没有现成工具」之后，交给引擎去造的那份规格。

    有这份规格，就表示这一次是**造**，不是**用**：``command`` 必为空。
    """

    tool_name: str = ""
    tool_intent: str = ""
    params_schema: Mapping[str, Any] = field(default_factory=dict)
    expected_output: str = ""
    cost_estimate: float | None = None
    feedback_plan: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolRequest:
    """调用方（05 / 自省）发给工具使用模块的请求。字段见设计文档 §9.1。"""

    request_id: str = field(default_factory=new_id)
    subject_id: str = ""
    activity_id: str = ""
    turn: int = 0
    origin: ToolOrigin = ToolOrigin.EXTERNAL_05
    need: str = ""  # 为什么用工具（人话）
    template: str = ""  # 匠石侧定义模板名（槽位说明）
    command: str = ""  # 引擎侧工具内容名（Pi command / skill）；造工具时为空
    create: CreateToolSpec | None = None  # 非空 = 这一次是「造」，不是「用」
    field_ref: Mapping[str, str] = field(default_factory=dict)  # 现场引用；206 提示词保留，不删
    params: Mapping[str, Any] = field(default_factory=dict)
    budget: Budget = field(default_factory=Budget)
    permission: PermissionContext = field(default_factory=PermissionContext)
    expected_result: str = ""  # 期望结果形态（一句话描述）
    ask: AskMode = AskMode.EXECUTE
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolEstimate:
    """使用前：代价 / 好处（报价）。"""

    cost_est: float | None = None
    time_est_ms: int | None = None
    resource_est: Mapping[str, float] = field(default_factory=dict)
    benefit: str = ""  # 用了能带来什么
    downside: str = ""  # 代价 / 风险
    need_confirm: bool = False


@dataclass(frozen=True)
class ToolProgress:
    """使用中：具体情况。"""

    stage: str = ""
    step: str = ""
    partial: str = ""  # 迄今的部分结果
    cost_used: float | None = None
    time_used_ms: int | None = None
    resource_used: Mapping[str, float] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class ToolResult:
    """使用后：结果与是否达到理想。"""

    status: ToolStatus = ToolStatus.OK
    result: Mapping[str, Any] = field(default_factory=dict)
    summary: str = ""  # 一句人话（给认知回灌）
    ideal: bool = False  # 是否达到理想结果
    ideal_note: str = ""  # 未达理想时差在哪
    cost: float | None = None
    time_ms: int | None = None
    resource: Mapping[str, float] = field(default_factory=dict)
    execution: tuple[Mapping[str, Any], ...] = ()  # 内部轨迹（steps / session 引用）
    error: str = ""


@dataclass(frozen=True)
class ToolFeedback:
    """过程反馈信封。一次工具使用 = 一串 ToolFeedback（estimate / progress / result）。"""

    event_id: str = field(default_factory=new_id)
    request_id: str = ""
    kind: FeedbackKind = FeedbackKind.RESULT
    at: datetime = field(default_factory=utc_now)
    version: str = "v1"
    estimate: ToolEstimate | None = None
    progress: ToolProgress | None = None
    result: ToolResult | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)
