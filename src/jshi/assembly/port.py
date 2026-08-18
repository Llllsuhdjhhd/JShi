from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class AssemblyContext:
    """一次初始装载的组装请求（03 传给各装载源）。"""

    subject_id: str
    input_text: str
    object_id: str | None = None
    context_view: object | None = None
    active_zone: object | None = None
    budget_extra: int = 4
    recall_level: int = 1
    focus: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssemblyFragment:
    """统一装载片段：各源输出的归一化视图（进模型上下文）。"""

    source: str  # identity | activity | personal | memory | epistemic | object
    id: str
    content: str
    kind: str
    status: str = "active"
    importance: float = 1.0
    source_ids: tuple[str, ...] = ()
    always: bool = False


@dataclass(frozen=True)
class LoadResult:
    """装载源的一次返回：统一片段 + 可选原始条目（内部审计用）。"""

    fragments: tuple[AssemblyFragment, ...] = ()
    raw_items: tuple[object, ...] = ()


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
    """统一装载能力：每个装载源实现同一协议，03 不依赖各源内部接口。"""

    name: str
    status: str

    def load(self, ctx: AssemblyContext) -> LoadResult: ...
