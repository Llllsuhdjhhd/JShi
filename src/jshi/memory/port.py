from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Sequence

from jshi.textutil import query_terms

if TYPE_CHECKING:
    from jshi.subject.domain import HistoryKind, HistoryRecord
    from jshi.subject.repository import SubjectRepository


def recall_level_limit(level: int) -> int:
    """把 1–9 回忆档位映射为最小实现的返回片段上限（占位）。

    1–3 线索级 → 3；4–6 情境级 → 5；7–9 深挖级 → 8。
    越界档位收敛到最近的合法档位。
    """

    bounded = max(1, min(9, int(level)))
    if bounded <= 3:
        return 3
    if bounded <= 6:
        return 5
    return 8


def _fact_mentions_object(record: object, object_id: str) -> bool:
    content = getattr(record, "content", None) or {}
    if str(content.get("object_id", "")) == object_id:
        return True
    listed = content.get("object_ids") or ()
    return object_id in listed


@dataclass(frozen=True)
class RecalledFragment:
    """One recalled past fragment shown in the current-state working set.

    形状对齐《记忆层契约》/ 09：``event_id`` 是稳定记忆单元标识（去重键），
    ``text`` 是原文（溯源），``content`` 是按 ``summary_level`` 选出的摘要文本
    （供预算 / 组装）。``event_type`` 是进程内实现的附加字段，非契约；真实
    记忆后端接入后不依赖它。
    """

    event_id: str
    event_type: str
    text: str
    content: str = ""  # 摘要文本（按 summary_level）；进程内实现缺省为原文
    summary_level: str | None = None  # L1 等；进程内实现无多级摘要，为 None
    kind: str = "fact"
    object_id: str | None = None
    source_ids: tuple[str, ...] = ()
    score: float = 0.0


class MemoryPort(Protocol):
    """Optional, replaceable memory backend.

    Project-internal history satisfies the minimal experiment. External engines
    (for example REMS) may implement the same narrow recall/remember surface
    later; they are not required by the architecture.
    """

    def remember_fact(
        self, subject_id: str, event_type: str, text: str, source_ids: tuple[str, ...] = ()
    ) -> str:
        """Persist a factual fragment; return its id."""

    def recall(
        self,
        subject_id: str,
        query: str,
        *,
        limit: int | None = None,
        object_id: str | None = None,
        level: int = 1,
        anchor_event_ids: tuple[str, ...] = (),
    ) -> Sequence[RecalledFragment]:
        """Return a bounded working set of related past fragments."""


class InProcessHistoryMemory:
    """Minimal memory adapter over the subject fact history store."""

    def __init__(self, repository: SubjectRepository) -> None:
        self._repository = repository

    def remember_fact(
        self, subject_id: str, event_type: str, text: str, source_ids: tuple[str, ...] = ()
    ) -> str:
        from jshi.subject.domain import HistoryKind, HistoryRecord

        record = HistoryRecord(
            subject_id=subject_id,
            kind=HistoryKind.FACT,
            event_type=event_type,
            content={"text": text},
            source_ids=source_ids,
        )
        self._repository.add_history(record)
        return record.id

    def recall(
        self,
        subject_id: str,
        query: str,
        *,
        limit: int | None = None,
        object_id: str | None = None,
        level: int = 1,
        anchor_event_ids: tuple[str, ...] = (),
    ) -> Sequence[RecalledFragment]:
        from jshi.subject.domain import HistoryKind

        # Transparent first implementation: recent facts, lightly filtered by
        # token/bigram overlap and, when available, object identity.
        cap = limit if limit is not None else recall_level_limit(level)
        # anchor_event_ids 仅供 05 协议兼容；07 事件本体独立前不参与过滤。
        recent = self._repository.list_history(
            subject_id, kind=HistoryKind.FACT, limit=max(cap * 3, cap)
        )
        if object_id:
            recent = [
                record
                for record in recent
                if _fact_mentions_object(record, object_id)
            ]
        tokens = query_terms(query)
        scored: list[tuple[float, HistoryRecord]] = []
        for record in recent:
            text = str(record.content.get("text", ""))
            hay = text.lower()
            score = float(
                sum(1 for token in tokens if token in hay) if tokens else 0
            )
            # Keep chronological presence even without lexical hit.
            scored.append((score, record))
        scored.sort(
            key=lambda item: (item[0], item[1].created_at), reverse=True
        )
        chosen = [record for score, record in scored if score > 0][:cap]
        if not chosen:
            chosen = [record for _, record in scored[:cap]]
        scores_by_id = {record.id: score for score, record in scored}
        return tuple(
            RecalledFragment(
                event_id=record.id,
                event_type=record.event_type,
                text=str(record.content.get("text", "")),
                # 进程内实现没有多级摘要：content 落回原文（真正的摘要档位由
                # 外部记忆后端接入后提供）。
                content=str(record.content.get("text", "")),
                summary_level=None,
                kind=record.kind.value,
                object_id=(
                    str(record.content["object_id"])
                    if record.content.get("object_id") is not None
                    else None
                ),
                source_ids=(record.id, *record.source_ids),
                score=scores_by_id.get(record.id, 0.0),
            )
            for record in chosen
        )
