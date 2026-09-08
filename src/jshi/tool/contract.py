"""工具使用契约：调用方（05 / 自省）与工具使用模块之间的数据结构。

对齐《doc/design/工具使用.md》：
- 反馈覆盖工具使用全流程：使用前（estimate）、使用中（progress）、使用后（result）。
- 字段只增不改 + ``version`` + 开放 ``meta``：方便日后演进，不破坏旧消费方。
- 工具目录 / 包管理归引擎（如 Pi），不在匠石侧自建注册表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class ToolOrigin(StrEnum):
    """哪一面触发这次工具使用。"""

    EXTERNAL_05 = "external_05"  # 外部认知（05 主流程）
    INTROSPECTION = "introspection"  # 自省（内部活动）


class AskMode(StrEnum):
    """调用方希望怎么走：先报价再决定，或直接执行。"""

    PROPOSE_ONLY = "propose_only"
    EXECUTE = "execute"


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


@dataclass(frozen=True)
class ToolRequest:
    """调用方（05 / 自省）发给工具使用模块的请求。字段见设计文档 §9.1。"""

    request_id: str = field(default_factory=new_id)
    subject_id: str = ""
    activity_id: str = ""
    turn: int = 0
    origin: ToolOrigin = ToolOrigin.EXTERNAL_05
    need: str = ""  # 为什么用工具（人话）
    template: str = ""  # 选哪个模板（工具定义方式，可替换）
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
