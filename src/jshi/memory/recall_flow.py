"""Recall orchestration shared by assembly, cognition, and background evaluation."""
from __future__ import annotations

from dataclasses import replace
import logging
from itertools import zip_longest
from time import perf_counter

from jshi.core.params import reflection_param as param
from .contracts import RecallCandidateBatch
from .selection import select_candidates, applicable
from .port import RecalledFragment

logger = logging.getLogger(__name__)


class RecallFlow:
    def __init__(self, memory, store, working_set):
        self.memory, self.store, self.working_set = memory, store, working_set

    def load_candidates(self, request, extra_queries=(), legacy_excerpts=()):
        started = perf_counter()
        rows, reused, query_runs, errors = [], [], [], []
        pools = []
        version = 0
        if param('RECALL_WORKING_ENABLED'):
            try:
                version, reused = self.working_set.load_scope(request,
                    ttl=float(param('RECALL_WORKING_TTL_SECONDS')),
                    similarity=float(param('RECALL_REUSE_SIMILARITY')), reserve=request.mode == 2)
            except Exception:
                logger.exception('recall working set read failed')
        if not request.enabled or not request.query_text.strip():
            reused = ()
        if request.enabled and request.query_text.strip():
            queries = tuple(dict.fromkeys((request.query_text, *extra_queries)))[:int(param('RECALL_QUERY_LIMIT'))]
            for query in queries:
                if not query.strip():
                    continue
                sub = replace(request, query_text=query,
                              object_ids=request.object_ids if query == request.query_text else (),
                              interlocutor_object_id=request.interlocutor_object_id if query == request.query_text else None)
                query_started = perf_counter()
                try:
                    fn = getattr(self.memory, 'recall_candidates', None)
                    result = tuple(fn(sub) if callable(fn) else self.memory.recall(
                        sub.subject_id, sub.query_text, object_ids=sub.object_ids,
                        interlocutor_object_id=sub.interlocutor_object_id,
                        level=sub.level, limit=sub.candidate_limit))
                    rows.extend(result)
                    pools.append(result)
                    query_runs.append({'query': query, 'status': 'completed' if result else 'empty',
                                       'returned': len(result),
                                       'elapsed_ms': round((perf_counter() - query_started) * 1000, 3)})
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    query_runs.append({'query': query, 'status': 'error', 'returned': 0,
                                       'error': type(exc).__name__,
                                       'elapsed_ms': round((perf_counter() - query_started) * 1000, 3)})
                    logger.exception('recall candidate query failed')
        if request.enabled and request.object_ids:
            legacy = []
            for ref, text in legacy_excerpts:
                if (ref.startswith('memory:') and text.strip()
                        and applicable(request.query_text, text, '', float(param('RECALL_REUSE_SIMILARITY')))):
                    legacy.append(RecalledFragment(ref.removeprefix('memory:'), 'legacy_excerpt', text,
                        content='旧摘录（归属及版本待核实）：' + text, kind='recall_excerpt', source_ids=(ref,)))
            pools.append(tuple(legacy))
            rows.extend(legacy)
        # Fresh representations win over cached ones, without inventing corrections.
        fresh_ids = {item.event_id for item in rows}
        reused = tuple(item for item in reused if item.event_id not in fresh_ids)
        # Query lanes and relevant retained evidence get a chance within the same cap.
        lanes = [pool for pool in (*pools, reused) if pool]
        merged = tuple(item for group in zip_longest(*lanes) for item in group if item is not None)
        invalidated = tuple(dict.fromkeys(
            ref for item in merged
            for ref in (*item.supersedes, *((item.event_id,) if item.validity == 'superseded' else ()))
        ))
        candidates, skipped = select_candidates(merged,
            max_items=request.candidate_limit, max_chars=int(param('RECALL_CANDIDATE_CHARS')))
        status = ('disabled' if not request.enabled else 'empty_query' if not request.query_text.strip()
                  else 'partial_error' if errors and rows else 'error' if errors
                  else 'completed' if candidates else 'empty')
        return RecallCandidateBatch(request=request, candidates=candidates,
            invalidated_ids=invalidated,
            reused_ids=tuple(item.event_id for item in reused),
            base_version=version, status=status, error=','.join(errors), query_runs=tuple(query_runs),
            skipped=skipped, truncated=any(reason == 'budget' for _, reason in skipped) or len(rows) >= request.candidate_limit,
            elapsed_ms=round((perf_counter() - started) * 1000, 3))

    def record_visible(self, batch, visible):
        self.store.observe_run(batch, visible)

    def complete(self, batch, visible, response, reference_map):
        from dataclasses import asdict
        from jshi.models import MemoryRatings
        visible_ids = {item.event_id for item in visible}
        ratings = getattr(response, 'memory_ratings', MemoryRatings())
        valid = []
        invalid = set(batch.invalidated_ids)
        invalid.update(ref for item in batch.candidates for ref in item.supersedes)
        for item in ratings.items:
            ident = reference_map.get(item.ref, '')
            if ident not in visible_ids:
                continue
            valid.append(replace(item, ref=ident))
            if item.misleading:
                invalid.add(ident)
        normalized = replace(ratings, items=tuple(valid))
        plan = [{'channel': item.channel, 'text': item.text}
                for item in response.response_plan.items]
        self.store.link_run_outcome(batch.request.run_id, plan,
                                    asdict(normalized) if normalized.has_content() else {})
        if batch.request.mode == 2 and param('RECALL_WORKING_ENABLED'):
            committed = self.working_set.commit_visible(batch.request, visible,
                expected_version=batch.base_version, invalid_ids=invalid,
                max_items=int(param('RECALL_WORKING_ITEMS')),
                ttl=float(param('RECALL_WORKING_TTL_SECONDS')))
            self.store.note_working_result(batch.request.run_id, committed)
        return normalized
