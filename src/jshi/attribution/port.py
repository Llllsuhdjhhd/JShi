from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Sequence

if TYPE_CHECKING:
    from jshi.subject.domain import Activity, PersonalItem


# 已废弃（02 起不再接线）：归属判断由活跃区与事件装载取代。
# 保留本文件供旧测试与回退参考，待 07 统一清理。


@dataclass(frozen=True)
class AttributionResult:
    """归属判断结果：本输入归属到哪些已有主体面关切。"""

    concern_ids: tuple[str, ...] = ()
    confidence: float = 0.0
    basis: tuple[str, ...] = ()
    status: str = "none"  # none | candidate（语义确认接入后可为 confirmed）


class AttributionPort(Protocol):
    """归属判断系统：在组装之前，用最小上下文判断输入归属到哪个已有关切。"""

    def judge(
        self,
        subject_id: str,
        input_text: str,
        standing_constraints: Sequence[PersonalItem],
        recent_activity: Activity | None = None,
    ) -> AttributionResult: ...


class PlaceholderAttribution:
    """占位实现：仅结构候选（唯一关切延续 + 文本重叠），不做模型语义确认。"""

    name = "placeholder-attribution"

    def judge(
        self,
        subject_id: str,
        input_text: str,
        standing_constraints: Sequence[PersonalItem],
        recent_activity: Activity | None = None,
    ) -> AttributionResult:
        concerns = [
            item
            for item in standing_constraints
            if item.kind.value == "concern"
        ]
        text = input_text.strip()
        if not text or not concerns:
            return AttributionResult()

        # 结构候选 1：唯一活跃关切 + 短输入 → 视为延续
        if len(concerns) == 1 and len(text) <= 20:
            concern = concerns[0]
            return AttributionResult(
                concern_ids=(concern.id,),
                confidence=0.5,
                basis=("single_active_concern", "short_input"),
                status="candidate",
            )

        # 结构候选 2：输入包含关切文本的显著片段（词汇重叠）
        tokens = [token for token in text.split() if len(token) >= 2] or [text]
        hits = [
            concern
            for concern in concerns
            if any(token in concern.content for token in tokens)
        ]
        if hits:
            return AttributionResult(
                concern_ids=tuple(concern.id for concern in hits),
                confidence=min(1.0, 0.3 + 0.2 * len(hits)),
                basis=("lexical_overlap",),
                status="candidate",
            )
        return AttributionResult()
