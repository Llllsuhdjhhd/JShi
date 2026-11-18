from __future__ import annotations

from typing import Sequence
from dataclasses import replace
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
        _candidate_mode: bool = False,
    ) -> Sequence[RecalledFragment]:
        context_object = interlocutor_object_id or object_id
        store = self.reflection_store
        adjustments = {}
        if store is not None and (not _candidate_mode or param('RECALL_FEEDBACK_ENABLED')):
            try:
                adjustments = store.adjustments(subject_id, query, context_object, object_ids=object_ids)
            except Exception:
                logging.getLogger(__name__).exception('reflection feedback read failed')
        positive = tuple(key for key, value in adjustments.items() if value > 0)
        expanded = limit
        if adjustments and not _candidate_mode and limit is not None and limit > 0:
            expanded = limit * int(param('REFLECTION_CANDIDATE_MULTIPLIER'))
        links = getattr(self, 'person_links', None)
        requested = tuple(dict.fromkeys(filter(None, (interlocutor_object_id, object_id, *object_ids))))
        scope, project = links.recall_scope(subject_id, requested) if links is not None else (requested, None)
        added = set(scope) - set(requested)
        # Gather a single pool with the previous per-person candidate allowance.
        # Filtering/projection happens before the public result limit, not per link.
        candidate_limit = expanded
        if added and expanded is not None:
            candidate_limit = expanded * (1 + len(added))
        fragments = tuple(self._backend.recall(
            subject_id,
            query,
            limit=candidate_limit,
            object_id=object_id,
            object_ids=scope if added else object_ids,
            interlocutor_object_id=interlocutor_object_id,
            level=level,
            anchor_event_ids=tuple(dict.fromkeys((*anchor_event_ids, *positive))),
        ))
        if project is not None:
            # A link may have been revoked while the backend was retrieving.
            project = links.projector(subject_id)
            combined={}
            for item in fragments:
                projected=project(item.object_id,item.source_ids)
                # Added old identities are only a search scope, not an identity assertion.
                if item.object_id in added and projected not in requested:
                    continue
                changed = projected != item.object_id
                combined.setdefault(item.event_id, replace(item,object_id=projected,
                    origin_object_id=(item.origin_object_id or item.object_id) if changed else item.origin_object_id,
                    identity_basis='provenance_link' if changed else item.identity_basis,
                    interlocutor=project(item.interlocutor,item.source_ids),
                    interlocutor_object_ids=tuple(dict.fromkeys(project(p,item.source_ids) for p in item.interlocutor_object_ids)),
                    query_object_role=('interlocutor' if projected==interlocutor_object_id else 'involved')
                        if changed and projected in requested else item.query_object_role))
            fragments=tuple(combined.values())
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
                if not _candidate_mode and not getattr(self._reflection_local, 'internal', False):
                    store.observe(subject_id, query, context_object, fragments)
            except Exception:
                logging.getLogger(__name__).exception('reflection recall enrichment failed')
        return tuple(result)

    def recall_candidates(self, request):
        """One bounded pool. Expansion belongs to the caller, never multiplies twice."""
        return self.recall(request.subject_id, request.query_text,
                           object_ids=request.object_ids,
                           interlocutor_object_id=request.interlocutor_object_id,
                           level=request.level, limit=request.candidate_limit,
                           _candidate_mode=True)

    def prepare_recall(self, subject_id, query, object_id):
        """Explicit read-only backend capability; never falls back to reinforcing recall."""
        if getattr(self,'person_links',None) is not None and self.person_links.links(subject_id):
            return (), {'__person_links__':'refresh_required'}
        fn = getattr(self._backend, 'prepare_recall', None)
        return fn(subject_id, query, object_id) if callable(fn) else ((), {})

    def validate_prepared(self, versions):
        if '__person_links__' in versions: return False
        fn = getattr(self._backend, 'validate_prepared', None)
        return bool(fn(versions)) if callable(fn) else not versions

    def memory_bytes(self, subject_id: str) -> int:
        fn = getattr(self._backend, 'memory_bytes', None)
        return int(fn(subject_id)) if callable(fn) else 0

    def ingest_batch(self, batch: MemoryBatch) -> BackendIngestResult:
        result=self._backend.ingest_batch(batch)
        links=getattr(self,'person_links',None)
        if links is not None:
            for item in batch.experiences:
                links.provenance(batch.subject_id,item.segment_id,item.source_ids)
                for event in result.stored_marks.get(item.segment_id,()):
                    links.provenance(batch.subject_id,event,(item.segment_id,))
        return result

    def portrait(self, subject_id: str, object_id: str) -> dict | None:
        """读取后端已形成的人物描述肖像。"""
        return self._backend.portrait(subject_id, object_id)
