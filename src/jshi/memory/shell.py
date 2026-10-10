from __future__ import annotations

from typing import Sequence
import logging
import threading
from jshi.core.params import reflection_param as param

from .backend import MemoryBackendPort
from .contracts import BackendIngestResult, MemoryBatch
from .port import RecalledFragment


class MemoryShell:
    """09 薄壳：主流程只依赖此端口，内部适配可替换后端。"""

    name = "memory-shell"

    def __init__(self, backend: MemoryBackendPort) -> None:
        self._backend = backend
        self.reflection_store = None
        self._reflection_local = threading.local()

    def remember_fact(
        self,
        subject_id: str,
        event_type: str,
        text: str,
        source_ids: tuple[str, ...] = (),
    ) -> str:
        return self._backend.remember_fact(
            subject_id,
            event_type,
            text,
            source_ids,
        )

    def recall(
        self,
        subject_id: str,
        query: str,
        *,
        limit: int | None = None,
        object_id: str | None = None,
        object_ids: tuple[str, ...] = (),
        interlocutor_object_id: str | None = None,
        level: int = 1,
        anchor_event_ids: tuple[str, ...] = (),
    ) -> Sequence[RecalledFragment]:
        context_object = interlocutor_object_id or object_id
        store = self.reflection_store
        adjustments = {}
        if store is not None:
            try:
                adjustments = store.adjustments(subject_id, query, context_object)
            except Exception:
                logging.getLogger(__name__).exception('reflection feedback read failed')
        positive = tuple(key for key, value in adjustments.items() if value > 0)
        expanded = limit
        if adjustments and limit is not None and limit > 0:
            expanded = limit * int(param('REFLECTION_CANDIDATE_MULTIPLIER'))
        fragments = tuple(self._backend.recall(
            subject_id,
            query,
            limit=expanded,
            object_id=object_id,
            object_ids=object_ids,
            interlocutor_object_id=interlocutor_object_id,
            level=level,
            anchor_event_ids=tuple(dict.fromkeys((*anchor_event_ids, *positive))),
        ))
        cap = len(fragments) if limit is None else max(0, limit)
        # 保留后端相关度不变，以归一化排名为基线，有限的反馈只改最终顺序。
        if adjustments:
            count = max(1, len(fragments))
            fragments = tuple(item for _, item in sorted(enumerate(fragments),
                key=lambda pair: -(1 - pair[0] / count + adjustments.get(pair[1].event_id, 0))))
        result = list(fragments[:cap])
        if store is not None:
            try:
                quota = min(cap, max(1, int(cap * float(param('REFLECTION_RECALL_RATIO'))))) if cap else 0
                insights = store.recall_insights(subject_id, query, context_object, quota,
                                               allowed_objects=object_ids if not context_object else ())
                if insights:
                    result = result[:max(0, cap - len(insights))]
                    # 在旧经历之间留少量新认识的位置，避免全放尾部后被组装预算裁掉。
                    for index, insight in enumerate(insights):
                        result.insert(min(len(result), 2 + index * max(1, int(1 / float(param('REFLECTION_RECALL_RATIO'))))), insight)
                if not getattr(self._reflection_local, 'internal', False):
                    store.observe(subject_id, query, context_object, fragments)
            except Exception:
                logging.getLogger(__name__).exception('reflection recall enrichment failed')
        return tuple(result)

    def memory_bytes(self, subject_id: str) -> int:
        fn = getattr(self._backend, 'memory_bytes', None)
        return int(fn(subject_id)) if callable(fn) else 0

    def ingest_batch(self, batch: MemoryBatch) -> BackendIngestResult:
        return self._backend.ingest_batch(batch)

    def portrait(self, subject_id: str, object_id: str) -> dict | None:
        """读取后端已形成的人物描述肖像。"""
        return self._backend.portrait(subject_id, object_id)
