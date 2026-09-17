from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Sequence

from jshi.models import RecallEvaluation, RecallRequest

from .port import MemoryPort, RecalledFragment

if TYPE_CHECKING:
    from jshi.subject.repository import SubjectRepository


@dataclass(frozen=True)
class RecallExecution:
    """一轮上下文补充召回的中间结果，由记忆侧协调器产生。"""

    round: int
    request: dict[str, object]
    duration_ms: float
    returned_count: int
    fresh: tuple[RecalledFragment, ...]
    fresh_ids: tuple[str, ...]
    truncated: bool = False
    metrics_id: str = ""


class RecallCoordinator:
    """09 侧召回编排：执行 MemoryPort.recall，并记录回忆侧指标。

    05 只产出 RecallRequest 候选；本协调器负责实际检索、
    工作集补充片段去重、`recall_extended` / `recall_metrics` /
    `recall_evaluated` / `recall_reference` 的记忆侧记录。
    """

    def __init__(
        self,
        repository: SubjectRepository,
        memory: MemoryPort,
    ) -> None:
        self._repository = repository
        self._memory = memory

    def execute_round(
        self,
        *,
        subject_id: str,
        activity_id: str,
        perception_id: str,
        round_number: int,
        requests: Sequence[RecallRequest],
        known_ids: set[str],
    ) -> RecallExecution:
        from jshi.subject.domain import HistoryKind, HistoryRecord

        if not requests:
            raise ValueError("recall round requires at least one request")

        start = time.perf_counter()
        fresh: list[RecalledFragment] = []
        returned_count = 0
        for request in requests:
            fragments = tuple(
                self._memory.recall(
                    subject_id,
                    request.query,
                    limit=request.budget or 3,
                    object_id=(
                        request.object_ids[0] if request.object_ids else None
                    ),
                    level=request.level,
                    anchor_event_ids=request.anchor_event_ids,
                )
            )
            returned_count = len(fragments)
            new_fragments = [
                item for item in fragments if item.event_id not in known_ids
            ]
            for item in new_fragments:
                known_ids.add(item.event_id)
                fresh.append(item)

            if new_fragments:
                self._repository.add_history(
                    HistoryRecord(
                        subject_id=subject_id,
                        kind=HistoryKind.SUBJECT,
                        event_type="recall_extended",
                        content={
                            "activity_id": activity_id,
                            "request": {
                                "query": request.query,
                                "budget": request.budget,
                                "object_ids": list(request.object_ids),
                                "anchor_event_ids": list(
                                    request.anchor_event_ids
                                ),
                            },
                            "recalled_event_ids": [
                                item.event_id for item in new_fragments
                            ],
                        },
                        source_ids=(
                            perception_id,
                            *(item.event_id for item in new_fragments),
                        ),
                    )
                )

        first = requests[0]
        return RecallExecution(
            round=round_number,
            request={
                "query": first.query,
                "budget": first.budget,
                "level": first.level,
                "object_ids": list(first.object_ids),
                "anchor_event_ids": list(first.anchor_event_ids),
            },
            duration_ms=round((time.perf_counter() - start) * 1000, 3),
            returned_count=returned_count,
            fresh=tuple(fresh),
            fresh_ids=tuple(item.event_id for item in fresh),
        )

    def commit_metrics(
        self,
        *,
        subject_id: str,
        activity_id: str,
        executions: Sequence[RecallExecution],
        truncated: bool,
    ) -> tuple[RecallExecution, ...]:
        from jshi.subject.domain import HistoryKind, HistoryRecord

        last_round = max((entry.round for entry in executions), default=0)
        committed: list[RecallExecution] = []
        for entry in executions:
            is_truncated = truncated and entry.round == last_round
            record = HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="recall_metrics",
                content={
                    "activity_id": activity_id,
                    "round": entry.round,
                    "request": entry.request,
                    "duration_ms": entry.duration_ms,
                    "returned_count": entry.returned_count,
                    "truncated": is_truncated,
                },
                source_ids=entry.fresh_ids,
            )
            self._repository.add_history(record)
            committed.append(
                replace(
                    entry,
                    truncated=is_truncated,
                    metrics_id=record.id,
                )
            )
        return tuple(committed)

    def record_evaluation(
        self,
        *,
        subject_id: str,
        activity_id: str,
        execution: RecallExecution,
        evaluation: RecallEvaluation,
    ) -> None:
        from jshi.subject.domain import HistoryKind, HistoryRecord

        if not execution.metrics_id:
            return
        self._repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="recall_evaluated",
                content={
                    "activity_id": activity_id,
                    "round": execution.round,
                    "usefulness": evaluation.usefulness,
                    "redundant": evaluation.redundant,
                    "need_more": evaluation.need_more,
                    "level_feedback": evaluation.level_feedback,
                    "note": evaluation.note,
                },
                source_ids=(execution.metrics_id,),
            )
        )

    def record_reference(
        self,
        *,
        subject_id: str,
        activity_id: str,
        added_ids: Sequence[str],
        cited_ids: Sequence[str],
    ) -> None:
        from jshi.subject.domain import HistoryKind, HistoryRecord

        if not added_ids:
            return
        referenced = [
            event_id for event_id in added_ids if event_id in cited_ids
        ]
        self._repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="recall_reference",
                content={
                    "activity_id": activity_id,
                    "recalled_event_ids": list(added_ids),
                    "referenced_ids": referenced,
                    "reference_count": len(referenced),
                    "rate": round(len(referenced) / len(added_ids), 3),
                },
                source_ids=tuple(added_ids),
            )
        )
