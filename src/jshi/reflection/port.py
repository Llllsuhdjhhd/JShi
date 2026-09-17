"""300 自省：端口与统一信封。

对齐《doc/design/300-自省.md》：
- 每个触发源只做一件事——把自己的特殊性翻译成**统一信封**（``IntrospectionRequest``）；
  信封之后（队列 / 排程 / 现场 / 六问 / 产出）对源一无所知。
- 三档投入（低/中/高）× 三档及时性（急/尽快/缓）是两个独立维度；
  本刀判定仍用触发源默认值，判定权留 ``assess`` 气口。
- 产出只到候选：不写 08 / 100，不调 ``append_internal``（见《方案 300》§5）。

保留 ``ReflectionPort.reflect``（CLI 与既有测试的同步入口），新增 ``IntrospectionPort``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence
from uuid import uuid4

if TYPE_CHECKING:
    from jshi.experienceledger.port import ExperienceSegment
    from jshi.memory.port import RecalledFragment
    from jshi.subject.domain import CognitiveContent
    from jshi.subject.process import SubjectProcess


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


# 三个入口时刻（设计 §2.1）：源声明自己响应哪些
MOMENT_AFTER_ACTIVITY = "after_activity"
MOMENT_IDLE = "idle"
MOMENT_EXPLICIT = "explicit"

# 低档不调模型时，未评步骤的占位（"步骤不减、深度可变"）
NOT_EVALUATED = "（未评）"
NO_MATERIAL = "（材料不足）"


class IntrospectionTrigger(StrEnum):
    """哪个源发起这次自省。"""

    EVENT_RESULT = "event_result"  # 事件结果（工具没做成）
    EPISTEMIC_REVISED = "epistemic_revised"  # 认识被修正 / 驳回（现为人工迁移触发）
    IDLE = "idle"  # 闲时回顾
    MANUAL = "manual"  # 人工 / CLI
    # 预留（设计 §2）：boundary_touched / repeated_event / commitment_breach /
    # relationship_changed / governance / emotion


class IntrospectionUrgency(StrEnum):
    """及时性：什么时候必须做完（只改排程，不改"让路"）。"""

    URGENT = "urgent"  # 急：排最前，力争下一拍之前跑完
    SOON = "soon"  # 尽快：收尾后或下一个空闲窗口
    SLOW = "slow"  # 缓：等空闲 / 夜间，资源紧时可不跑


class IntrospectionLevel(StrEnum):
    """投入：做多深（低档不调模型，只落规则摘要）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IntrospectionStatus(StrEnum):
    QUEUED = "queued"
    DROPPED = "dropped"
    DONE = "done"
    FAILED = "failed"


_URGENCY_RANK = {
    IntrospectionUrgency.URGENT: 2,
    IntrospectionUrgency.SOON: 1,
    IntrospectionUrgency.SLOW: 0,
}
_LEVEL_RANK = {
    IntrospectionLevel.LOW: 0,
    IntrospectionLevel.MEDIUM: 1,
    IntrospectionLevel.HIGH: 2,
}


def urgency_rank(value: IntrospectionUrgency | str) -> int:
    return _URGENCY_RANK.get(IntrospectionUrgency(str(value)), 1)


def level_rank(value: IntrospectionLevel | str) -> int:
    return _LEVEL_RANK.get(IntrospectionLevel(str(value)), 1)


@dataclass(frozen=True)
class IntrospectionRequest:
    """统一信封：源特有的东西全部压进这几个字段（设计 §2.1）。"""

    subject_id: str
    trigger: IntrospectionTrigger
    entry_ref: str  # 条目：activity:<id> | segment:<id> | cognition:<id> | manual:<主题>
    entry_at: datetime  # 条目发生时间（"当时"的中心）
    entry_text: str = ""  # 条目文本（现场取窗与召回 query 的种子）
    level: IntrospectionLevel = IntrospectionLevel.MEDIUM
    urgency: IntrospectionUrgency = IntrospectionUrgency.SOON
    object_id: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    dedup_key: str = ""  # 主题键；空则用 entry_ref
    entry_at_also: datetime | None = None  # 第二个时间中心（如"迁移发生时"）
    origin: str = ""  # 触发细节，如 "manual_transition"
    request_id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    @property
    def theme(self) -> str:
        """冷却 / 去重用的主题键。"""
        return self.dedup_key or self.entry_ref

    @property
    def centers(self) -> tuple[datetime, ...]:
        if self.entry_at_also is None:
            return (self.entry_at,)
        return (self.entry_at, self.entry_at_also)


@dataclass(frozen=True)
class IntrospectionScene:
    """现场（设计 §4.1）：只读，拼出来的材料，不是结论。"""

    request: IntrospectionRequest
    window: tuple[datetime, datetime]
    segments: tuple["ExperienceSegment", ...] = ()
    recalled: tuple["RecalledFragment", ...] = ()
    text: str = ""
    source_ids: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.segments and not self.recalled


@dataclass(frozen=True)
class IntrospectionAnswer:
    """六问（⑤ 三个出口）。低档不调模型时，未评步骤放 ``NOT_EVALUATED``。"""

    what: str = ""
    choice_and_result: str = ""
    why: str = ""  # 归因：我能控制的 / 环境的 / 偶然的（+ 换位归因）
    better: str = ""
    next_time: str = ""  # ⑤a 条件—动作
    tool_need: bool = False  # ⑤b
    tool_capability: str = ""
    tool_when: str = ""
    tool_worth: str = ""
    interaction_need: Mapping[str, Any] | None = None  # ⑤c
    worth_learning: bool = False  # ⑥
    candidates: tuple[Mapping[str, Any], ...] = ()
    raw_text: str = ""
    degraded: bool = False  # 结构化解析失败，降级为原文
    mode: str = "model"  # model | rule | fallback


@dataclass(frozen=True)
class IntrospectionRun:
    """一次自省的落库结果。"""

    request: IntrospectionRequest
    scene_source_ids: tuple[str, ...]
    answer: IntrospectionAnswer
    activity_id: str
    cognitive_content_id: str
    status: IntrospectionStatus = IntrospectionStatus.DONE
    duration_ms: float = 0.0
    model_tag: str = ""
    reason: str = ""


@dataclass
class SourceContext:
    """一次 pump 的上下文。探测器**只读**它，不写任何东西。"""

    subject_id: str
    moment: str
    now: datetime
    repository: Any
    ledger: Any
    memory: Any
    hang_store: Any = None
    activity: Any = None  # 仅 after_activity 有
    plan: Any = None
    action_id: str = ""
    object_id: str | None = None


class IntrospectionSource(Protocol):
    """一个触发源 = 一个"只读探测器"。"""

    name: str
    moments: frozenset[str]

    def detect(self, context: SourceContext) -> Sequence[IntrospectionRequest]: ...


class ReflectionPort(Protocol):
    """反思与学习系统：回顾经历、继续未完成问题、重新理解旧记忆。"""

    def reflect(self, subject_id: str, prompt: str) -> "CognitiveContent": ...


class IntrospectionPort(Protocol):
    """300 的完整入口：入队、排程、执行、回看。"""

    def enqueue_from_sources(
        self,
        moment: str,
        subject_id: str,
        **context: Any,
    ) -> tuple[IntrospectionRequest, ...]: ...

    def enqueue_idle(
        self, subject_id: str, *, object_id: str | None = None
    ) -> IntrospectionRequest | None: ...

    def drain(
        self, subject_id: str | None = None, *, limit: int = 1
    ) -> tuple[IntrospectionRun, ...]: ...


class PlaceholderReflection:
    """占位实现：委托给主体流程的当前最小反思逻辑。

    保留给回退开关（``SubjectProcess(reflection=PlaceholderReflection(self))``）；
    旧实现落在 ``SubjectProcess._reflect_internal``。
    """

    name = "placeholder-reflection"

    def __init__(self, process: "SubjectProcess") -> None:
        self._process = process

    def reflect(self, subject_id: str, prompt: str) -> "CognitiveContent":
        return self._process._reflect_internal(subject_id, prompt)
