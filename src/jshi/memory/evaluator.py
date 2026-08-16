from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from jshi.models import RecallEvaluation

from .coordinator import RecallExecution

if TYPE_CHECKING:
    from jshi.subject.domain import CognitiveContent


class RecallEvaluatorPort(Protocol):
    """回忆评价过程：召回发生后收集材料，单独生成评价。"""

    def evaluate(
        self,
        *,
        subject_id: str,
        activity_id: str,
        execution: RecallExecution,
        thought: CognitiveContent,
    ) -> RecallEvaluation | None:
        """基于召回执行和最终 thought 生成评价；不可用时返回 None。"""


class RuleBasedRecallEvaluator:
    """最小独立评价过程：用来源链与返回条数做确定性评价。"""

    def evaluate(
        self,
        *,
        subject_id: str,
        activity_id: str,
        execution: RecallExecution,
        thought: CognitiveContent,
    ) -> RecallEvaluation:
        del subject_id, activity_id
        referenced = [
            event_id
            for event_id in execution.fresh_ids
            if event_id in thought.source_ids
        ]
        usefulness = "related" if referenced else "unrelated"
        need_more = execution.returned_count == 0
        return RecallEvaluation(
            usefulness=usefulness,
            redundant=bool(execution.fresh_ids) and not referenced,
            need_more=need_more,
            level_feedback="too_low" if need_more else "ok",
            note=(
                f"referenced {len(referenced)}/{len(execution.fresh_ids)} "
                "recalled fragments"
            ),
        )
