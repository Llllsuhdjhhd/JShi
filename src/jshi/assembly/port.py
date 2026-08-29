from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from jshi.experienceledger import ContextViewState


@dataclass(frozen=True)
class AssemblySpeaker:
    """① 已解析的说话人，供 03 对象源与记忆过滤。03 不再 resolve。"""

    object_id: str
    label: str
    aliases: tuple[str, ...] = ()
    status: str = "provisional"
    reason: str = ""


@dataclass(frozen=True)
class AssemblyContext:
    """一次初始装载的组装请求（03 传给各装载源）。"""

    subject_id: str
    input_text: str
    speaker: AssemblySpeaker | None = None
    context_view: ContextViewState | None = None
    recall_level: int = 1
    working_set_limit: int | None = None


@dataclass(frozen=True)
class AssemblyFragment:
    """统一装载片段：各源输出的归一化视图（进模型上下文）。"""

    source: str  # identity | object | activity | personal | memory
    id: str
    content: str
    kind: str
    status: str = "active"
    importance: float = 1.0
    source_ids: tuple[str, ...] = ()
    always: bool = False


@dataclass(frozen=True)
class LoadResult:
    """装载源的一次返回：统一片段 + 该源自己跳过的 id。"""

    fragments: tuple[AssemblyFragment, ...] = ()
    raw_items: tuple[object, ...] = ()
    skipped_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceLoadReport:
    """组装器汇总的装载报告（供记账与 14 分析）。"""

    source: str
    status: str  # implemented | placeholder
    loaded_ids: tuple[str, ...] = ()
    skipped_ids: tuple[str, ...] = ()
    budget: int = 0
    error: str | None = None


class AssemblySourcePort(Protocol):
    """统一装载能力：每个装载源实现同一协议，检索规则留在源模块。"""

    name: str
    status: str

    def load(self, ctx: AssemblyContext) -> LoadResult: ...
