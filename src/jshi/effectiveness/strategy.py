"""有效性分析输出的“策略”对象：一段召回策略，而不是一个分数。

设计原则（对齐“有 rem 支持”）：

- 本系统的对外“输出”是 ``MemoryRecallStrategy``——一套**召回参数**，而不是
  单一的质量分。质量分、coverage、unrelated 比例等只是**推导该策略所用的一组
  证据**，放在 ``evidence`` / ``findings`` 里，不作为头条结论。
- 策略字段对齐记忆引擎实际消费的召回入参，确保能落到 REM（Rems3）上：
  - ``level`` / ``default_level``：1–9 召回强度（`MemoryPort.recall` 与 REMS 都接受）。
  - ``limit``：召回预算（返回片段上限；目标 REM 用 `budget`/`limit`）。
  - ``summary_level``：REMS 多级摘要档位（如 "L1"）；引擎支持时生效，未接入则留空。
  - ``recall_mode``：策略取向 ``balanced`` | ``precision`` | ``coverage``。
    其中 ``precision`` 取向对应“少而准”（可能附带对象/线索路由），
    ``coverage`` 取向对应“缺啥补啥”，两者都映射到 ``level`` 的方向。
- ``basis`` 记录策略来源：``baseline:vN``（由示例库校准）或 ``heuristic``（无基准回退）。

本模块不 import 引擎，只在业务层描述“要什么样的召回”，由 09 薄壳落地。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from uuid import uuid4


def utc_now() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


_RECALL_MODES = frozenset({"balanced", "precision", "coverage"})


def _clamp_level(value: int) -> int:
    return max(1, min(9, int(value)))


@dataclass(frozen=True)
class MemoryRecallStrategy:
    """一段召回策略（可执行的召回参数 + 依据）。

    这是有效性分析器交给 09 / REM 的“策略”，不是分数。
    ``quality_score`` 等证据不在这里，而在 ``evidence``。
    """

    default_level: int = 1  # 1–9 召回强度；09 薄壳读取，送 `recall(level=...)`
    limit: int | None = None  # 召回预算（片段上限）；`recall(limit=...)`
    summary_level: str | None = None  # REMS 多级摘要档位（如 "L1"）；引擎支持时生效
    recall_mode: str = "balanced"  # balanced | precision | coverage
    confidence: float = 0.0  # 0..1，基于示例库校准
    reason: str = ""  # 为什么这样定
    evidence: tuple[Mapping[str, Any], ...] = ()  # 触发推导的信号 / 证据（含质量分）
    basis: str = "heuristic"  # "baseline:vN" | "heuristic"
    source_report_id: str = ""
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "default_level", _clamp_level(self.default_level))
        mode = (self.recall_mode or "balanced").strip().lower()
        object.__setattr__(self, "recall_mode", mode if mode in _RECALL_MODES else "balanced")
        object.__setattr__(
            self, "confidence", max(0.0, min(1.0, float(self.confidence)))
        )
        if self.limit is not None:
            object.__setattr__(self, "limit", max(0, int(self.limit)))

    @property
    def level(self) -> int:
        """与 ``default_level`` 同义；供 09 / 组装侧按 ``level`` 读取。"""
        return self.default_level

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_level": self.default_level,
            "limit": self.limit,
            "summary_level": self.summary_level,
            "recall_mode": self.recall_mode,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "evidence": [dict(item) for item in self.evidence],
            "basis": self.basis,
            "source_report_id": self.source_report_id,
            "updated_at": self.updated_at.isoformat() if self.updated_at else "",
        }

    def with_saved_ref(self, report_id: str) -> MemoryRecallStrategy:
        from dataclasses import replace

        return replace(self, source_report_id=report_id, updated_at=utc_now())


def strategy_to_report(o: MemoryRecallStrategy) -> dict[str, Any]:
    """落到 ``EffectivenessReport.strategy`` 的窄口（保留策略参数与依据）。"""
    return {
        "default_level": o.default_level,
        "limit": o.limit,
        "summary_level": o.summary_level,
        "recall_mode": o.recall_mode,
        "confidence": o.confidence,
        "basis": o.basis,
        "source_report_id": o.source_report_id,
    }
