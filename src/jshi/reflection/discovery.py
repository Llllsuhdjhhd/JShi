"""Select an evidence-backed question without running the six-question reflection."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
from typing import Protocol

from jshi.core.scene_text import BEIJING, observation_text
from jshi.experienceledger.port import ExperienceSegment, OutputKind, segment_view_text
from jshi.tool.contract import FeedbackKind, ToolFeedback
from jshi.tool.hang import HangRecord
from .port import IntrospectionLevel, IntrospectionRequest, IntrospectionTrigger, IntrospectionUrgency, urgency_rank


@dataclass(frozen=True)
class DiscoveryContext:
    subject_id: str
    source: str
    now: datetime
    cursor: int = 0
    next_cursor: int = 0
    segments: tuple[ExperienceSegment, ...] = ()
    baseline: tuple[ExperienceSegment, ...] = ()
    tool_results: tuple[tuple[HangRecord, ToolFeedback], ...] = ()
    signals: tuple[IntrospectionRequest, ...] = ()
    object_id: str | None = None
    deferred_segments: tuple[tuple[str, float], ...] = ()
    resolved_segments: tuple[str, ...] = ()
    missing_segments: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryBatch:
    requests: tuple[IntrospectionRequest, ...] = ()
    coverage: tuple[dict, ...] = ()


class DiscoveryAssistant(Protocol):
    def discover(self, context: DiscoveryContext) -> DiscoveryBatch: ...


def read_discovery_context(process, subject_id, source, cursor, *, now=None, limit=8, object_id=None,
                           due_segment_ids=()):
    """Read a bounded source page. After the first day, continue by arrival order."""
    now = now or datetime.now(BEIJING)
    if now.tzinfo is None:
        raise ValueError("discovery needs a timezone-aware time")
    limit = max(1, int(limit))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ctx = DiscoveryContext(subject_id, source, now, cursor, cursor, object_id=object_id)
    if source == 'tools':
        store = getattr(process, 'hang_store', None)
        reader = getattr(store, 'read_result_page', None)
        if not callable(reader):
            return ctx
        after = cursor or (int(start.timestamp()) * 1_000_000_000 - 1)
        rows = reader(subject_id, after=after, limit=limit, object_id=object_id)
        return replace(ctx, tool_results=tuple((record, item) for _, record, item in rows),
                       next_cursor=rows[-1][0] if rows else after)
    if source != 'ledger':
        raise ValueError(f"unknown discovery source: {source}")
    reader = getattr(process.activity_ledger, 'read_experience_page', None)
    if not callable(reader):
        return ctx
    # Zero is the uninitialized cursor. One means an empty ledger was actually
    # visited, so tomorrow's scan can still see late arrivals from yesterday.
    arrived = reader(subject_id, after_sequence=max(0, cursor - 1), limit=limit,
                     start=None if cursor else start, object_id=object_id)
    next_cursor = arrived[-1].sequence + 1 if arrived else cursor
    if not arrived and not due_segment_ids:
        if not cursor:
            older = reader(subject_id, limit=1, end=start, newest_first=True, object_id=object_id)
            return replace(ctx, next_cursor=1 + (older[0].sequence if older else 0))
        return ctx
    # Clock skew can place an earlier sequence in the future while later ones
    # are already observable. Save its ID and due time beside the scan cursor.
    # A receipt dated years ahead takes no time from subsequent ordinary pages.
    due = process.activity_ledger.experiences_by_ids(subject_id, due_segment_ids) if due_segment_ids else ()
    indexed = {r.segment_id: r for r in (*due, *arrived)
               if object_id is None or r.actor_object_id == object_id}
    deferred = {ref: r.occurred_at.timestamp() for ref, r in indexed.items() if r.occurred_at > now}
    ctx = replace(ctx, next_cursor=next_cursor, deferred_segments=tuple(deferred.items()),
                  resolved_segments=tuple(ref for ref in due_segment_ids if ref not in deferred),
                  missing_segments=tuple(ref for ref in due_segment_ids if ref not in indexed))
    rows = tuple(sorted((r for ref, r in indexed.items() if ref not in deferred), key=lambda r: r.sequence))
    if not rows:
        return ctx
    if cursor and not object_id and rows[0].output_kind == OutputKind.SUBJECT_REPLY:
        previous = reader(subject_id, after_sequence=max(0, rows[0].sequence - 2), limit=1)
        if (previous and previous[0].sequence == rows[0].sequence - 1
                and previous[0].occurred_at <= now
                and previous[0].output_kind == OutputKind.EXTERNAL_INPUT):
            rows = (*previous, *rows)
    # Baseline is a bounded set of comparable original records. Unknown identity
    # never inherits a different person's past; default rules infer no trait from counts.
    scopes = dict.fromkeys(
        (r.actor_object_id, r.occurred_at.astimezone(now.tzinfo).replace(hour=0, minute=0, second=0, microsecond=0))
        for r in rows if r.actor_object_id and r.output_kind == OutputKind.EXTERNAL_INPUT)
    past = {}
    for actor, day in scopes:
        sample = reader(subject_id, limit=max(1, limit // len(scopes)), start=day-timedelta(days=7),
                        end=day, newest_first=True, object_id=actor)
        past.update((r.segment_id, r) for r in sample)
    return replace(ctx, segments=tuple(rows), baseline=tuple(past.values()), next_cursor=next_cursor)


def signal_context(subject_id, requests, *, hang_store=None, now=None):
    """Resolve activity-close hints to the same tool result facts as idle rescan."""
    signals = []
    results = []
    for request in requests:
        if request.subject_id != subject_id:
            continue
        is_tool_hint = request.evidence.get('tool_failures') or any(
            part.get('tool_failures') for part in request.evidence.get('merged', ()) if isinstance(part, dict))
        if request.trigger != IntrospectionTrigger.EVENT_RESULT or not is_tool_hint:
            signals.append(request)
            continue
        if hang_store is None or not request.object_id:
            continue
        activity_id = request.entry_ref.removeprefix('activity:')
        reader = getattr(hang_store, 'result_records_for_activity', None)
        records = (reader(subject_id, activity_id, request.object_id) if callable(reader)
                   else hang_store.list_for(subject_id, request.object_id))
        for record in records:
            if record.subject_id != subject_id or record.field_ref.get('activity_id') != activity_id:
                continue
            # The store's person-link projection is display context. Both arrival
            # paths key this receipt by the owner recorded with the original fact.
            for item in record.feedback:
                if _is_result(item) and (item.result.status != 'ok' or
                                         (not item.result.ideal and item.result.ideal_note)):
                    results.append((record, item))
    return DiscoveryContext(subject_id, 'signals', now or datetime.now(BEIJING),
                            signals=tuple(signals), tool_results=tuple(results))


def _is_result(item):
    return (item.kind == FeedbackKind.RESULT and item.result is not None
            and not item.meta.get('recorded_time_unknown') and item.at.utcoffset() is not None)


def _failed(result):
    return result.status != 'ok' or (not result.ideal and bool(result.ideal_note))


def _select_tool_record(record, item):
    if _failed(item.result):
        return True
    # The same successful task is sampled on arrival and on replay. A later
    # recovery from an observed failure stays with that original question.
    for previous in record.feedback:
        if previous == item:
            break
        if _is_result(previous) and _failed(previous.result):
            return True
    return sha256(record.task_id.encode()).digest()[0] % 4 == 0


def requests_for_tool_record(subject, record, items):
    if record.subject_id != subject:
        return ()
    return tuple(_tool_request(record, item) for item in items if _is_result(item))


def _tool_request(record, item):
    result = item.result
    facts = {
        'need': record.need, 'status': str(result.status), 'summary': result.summary or result.error,
        'error': result.error, 'ideal_note': result.ideal_note, 'cost': result.cost,
        'time_ms': result.time_ms,
    }
    returned = dict(result.result)
    version_content = (facts, result.ideal, returned)
    digest = sha256(json.dumps(version_content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    ref = f'tool:{record.task_id}:{item.event_id}:{item.version}:{digest}'
    if returned:
        # Keep the original in HangStore. The question only carries a bounded
        # sample; never turn a cut-off result into a claim of full coverage.
        packed = json.dumps(returned, ensure_ascii=False, sort_keys=True)
        if len(packed) <= 2000:
            facts['actual_result'] = returned
        else:
            facts['actual_result'] = {'excerpt': str(returned.get('content') or packed)[:2000]}
            facts['actual_result_truncated'] = True
    facts.update(source_id=ref, task_id=record.task_id, event_id=item.event_id, at=item.at.isoformat())
    failed = _failed(result)
    topic = record.need.strip()[:90] or '这次任务'
    question = (f'为「{topic}」使用工具时，哪里与预期有差异？' if failed
                else f'为「{topic}」取得的结果，能核实原来的要求吗？')
    reason = '有实际的工具返回和未达预期的记录。' if failed else '普通工具事项轮到回看，可对照原要求和实际返回。'
    activity = record.field_ref.get('activity_id')
    return IntrospectionRequest(subject_id=record.subject_id, object_id=record.object_id,
        trigger=IntrospectionTrigger.EVENT_RESULT, origin='discovery', entry_at=item.at,
        entry_at_also=record.created_at, entry_ref=f'activity:{activity}' if activity else f'task:{record.task_id}',
        entry_text=question, dedup_key=f'tool-review:{record.task_id}', urgency=IntrospectionUrgency.SOON if failed
        else IntrospectionUrgency.SLOW, evidence={'source_versions':[ref], 'reason':reason, 'method':'review',
                                                 'tool_sample':_select_tool_record(record, item), 'tool_facts':[facts]})


def _ordinary_request(rows, baseline, tz):
    # Select a single person's contiguous stretch. A neighboring subject reply
    # is usable as context but never attributed to that person.
    first = rows[0]
    if first.output_kind == OutputKind.EXTERNAL_INPUT:
        actor = first.actor_object_id
    else:
        actor = None
    refs = [r.segment_id for r in rows]
    quote = observation_text(segment_view_text(first)).strip()
    if not quote or (len(rows) == 1 and len(quote) < 6):
        return None
    day = first.occurred_at.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    comparable = [r for r in baseline if actor and r.actor_object_id == actor
                  and r.output_kind == first.output_kind and day - timedelta(days=7) <= r.occurred_at < day]
    repeated = next((r for r in comparable if segment_view_text(r).strip() == segment_view_text(first).strip()), None)
    reason = '这段普通交往轮到浏览，原话可核对。'
    if repeated:
        question = f'两段交谈都提到「{quote[:60]}」，当时面对的是同一个问题吗？'
        reason = '同一来源的两段原话重复出现，可对照各自的情境。'
        refs.append(repeated.segment_id)
        basis = 'comparison'
    elif len(rows) >= 2 and rows[1].output_kind == OutputKind.SUBJECT_REPLY and actor:
        question = f'对方提到「{quote[:60]}」时，我接下来的回应怎样承接了原话？'
        basis = 'reply'
    else:
        question = f'在提到「{quote[:70]}」的这段经历里，有什么值得重新核对或保留？'
        basis = 'ordinary'
    return IntrospectionRequest(subject_id=first.subject_id, object_id=actor,
        trigger=IntrospectionTrigger.IDLE, origin='discovery', level=IntrospectionLevel.MEDIUM,
        urgency=IntrospectionUrgency.SLOW, entry_at=first.occurred_at, entry_ref=f'segment:{first.segment_id}',
        entry_text=question,
        entry_at_also=repeated.occurred_at if repeated else None,
        dedup_key=f'ordinary:{first.segment_id}', evidence={'reason':reason, 'segment_ids':refs, 'selection_basis':basis,
            'review_segments':[r.segment_id for r in rows],
            'baseline_sample_count':len(comparable), 'baseline_status':'sampled' if len(comparable) >= 2 else 'unknown',
            'source_versions':[f'segment:{ref}' for ref in refs], 'method':'compare' if repeated else 'review'})


def _ordinary_candidates(rows, baseline, tz):
    group = []
    for row in rows:
        usable = row.output_kind in (OutputKind.EXTERNAL_INPUT, OutputKind.SUBJECT_REPLY) and row.text_raw
        other_actor = row.output_kind == OutputKind.EXTERNAL_INPUT and group and (
            row.actor_object_id != group[0].actor_object_id or not row.actor_object_id)
        distant = group and abs((row.occurred_at-group[-1].occurred_at).total_seconds()) > 600
        interrupted = group and row.sequence != group[-1].sequence + 1
        if not usable or other_actor or distant or interrupted:
            if group:
                yield _ordinary_request(group, baseline, tz)
                group = []
        if usable:
            group.append(row)
    if group:
        yield _ordinary_request(group, baseline, tz)


def _select_ordinary(candidates, turn):
    """Keep one place for an ordinary sample when a more specific question exists."""
    preferred = []
    ordinary = []
    for basis in ('comparison', 'reply', 'ordinary'):
        peers = [r for r in candidates if r.evidence['selection_basis'] == basis]
        if peers:
            start = turn % len(peers)
            ordered = peers[start:] + peers[:start]
            (ordinary if basis == 'ordinary' else preferred).extend(ordered)
    if preferred and ordinary:
        return [preferred[0], ordinary[0]]
    return (preferred or ordinary)[:2]


class RuleDiscoveryAssistant:
    """First-pass rules; callers can inject a different DiscoveryAssistant."""
    def discover(self, context: DiscoveryContext) -> DiscoveryBatch:
        questions = []
        coverage = []
        tool_versions = set()
        for record, item in context.tool_results:
            if record.subject_id != context.subject_id:
                continue
            if not _is_result(item):
                raw_ref = f'tool:{record.task_id}:{item.event_id}'
                coverage.append({'source':'tools', 'listed':[raw_ref], 'seen':[raw_ref],
                                 'missing':['terminal_result_or_time']})
                continue
            request = _tool_request(record, item)
            version = request.evidence['source_versions'][0]
            if version in tool_versions:
                continue
            tool_versions.add(version)
            selected = request.evidence['tool_sample']
            if selected:
                questions.append(request)
            coverage.append({'source':'tools', 'listed':[request.entry_ref],
                             'seen':[version], 'selected':[version] if selected else [],
                             'reviewed_only':[] if selected else [version]})
        if context.source == 'tools' and not context.tool_results:
            coverage.append({'source':'tools','listed':[],'seen':[]})
        for signal in context.signals:
            if signal.evidence.get('source_versions'):
                selected = signal.evidence.get('tool_sample', True)
                if selected:
                    questions.append(signal)
                refs = signal.evidence['source_versions']
                coverage.append({'source':'signals','listed':[signal.entry_ref],
                                 'seen':refs, 'selected':refs if selected else [],
                                 'reviewed_only':[] if selected else refs})
                continue
            stamp = signal.entry_at.isoformat()
            version = signal.evidence.get('transition_ref') or f'{signal.entry_ref}@{stamp}'
            evidence = dict(signal.evidence, source_versions=[version])
            if signal.trigger == IntrospectionTrigger.EPISTEMIC_REVISED:
                evidence['reason'] = signal.evidence.get('reason') or '已有认识被修订或驳回。'
                text = f'对「{signal.entry_text[:100]}」的认识为何需要修订？'
            else:
                text = signal.entry_text
                evidence.setdefault('reason', '这项已有线索指向一段可回看的经历。')
            questions.append(replace(signal, entry_text=text, evidence=evidence))
            coverage.append({'source':'signals','listed':[signal.entry_ref],'seen':evidence['source_versions']})
        if context.source == 'ledger':
            candidates = [r for r in _ordinary_candidates(context.segments, context.baseline, context.now.tzinfo)
                          if r is not None]
            turn = context.next_cursor // max(1, len(context.segments))
            selected = _select_ordinary(candidates, turn)
            questions.extend(selected)
            chosen = {r.entry_ref for r in selected}
            refs = [row.segment_id for row in context.segments]
            deferred = [ref for ref, _ in context.deferred_segments]
            coverage.append({'source':'ledger','listed':[*refs, *deferred, *context.missing_segments],
                             'seen':refs, 'deferred':deferred, 'missing':list(context.missing_segments),
                             'selected':[r.entry_ref for r in selected],
                             'reviewed_only':[r.entry_ref for r in candidates if r.entry_ref not in chosen],
                             'baseline':[row.segment_id for row in context.baseline]})
        questions.sort(key=lambda request: urgency_rank(request.urgency), reverse=True)
        return DiscoveryBatch(tuple(questions), tuple(coverage))
