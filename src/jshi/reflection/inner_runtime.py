"""Discovery and six-question handoff inside the existing reflection executor."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from time import perf_counter

from jshi.core.scene_text import BEIJING
from jshi.core.params import introspection_enabled
from jshi.experienceledger.port import DELIVERY_PLANNED_AUDIO, OutputKind, SegmentStatus, segment_delivery
from .discovery import (
    RuleDiscoveryAssistant, read_discovery_context, requests_for_tool_record, signal_context,
)
from .sources.epistemic import EpistemicRevisedSource
from .port import (
    IntrospectionLevel, IntrospectionRequest, IntrospectionTrigger, IntrospectionUrgency,
    MOMENT_AFTER_ACTIVITY, new_id, utc_now,
)


class InnerActivityRuntime:
    def __init__(self, process, reflection, store, assistant=None):
        self.process = process
        self.reflection = reflection
        self.store = store
        self.questions = store.questions
        self.assistant = RuleDiscoveryAssistant() if assistant is None else assistant
        self.supported = all(callable(getattr(reflection, name, None)) for name in
                             ('collect_question', 'prepare_question', 'publish_question', 'detect'))
        self.store.discovery_supported = self.supported

    def mode(self):
        if not self.enabled:
            return 0
        return self.store.discovery_mode()

    @property
    def enabled(self):
        return self.supported and getattr(self.reflection, 'enabled', True) and introspection_enabled()

    def _adopt_legacy(self, subject):
        self.questions.import_legacy(subject, self.process.repository)
        queue = getattr(self.reflection, 'queue', None)
        if queue is not None:
            for request in queue.pending():
                if request.subject_id == subject:
                    self.questions.enqueue_legacy(request, queue, already_queued=True)

    def note_activity(self, subject, activity, plan, action_id, speaker, *, shadow):
        # The foreground records references only. An injected assistant may be expensive.
        requests = self.reflection.detect(
            MOMENT_AFTER_ACTIVITY, subject, activity=activity, plan=plan, action_id=action_id,
            object_id=speaker.object_id if speaker is not None else None)
        if requests:
            self.questions.put_signals(subject, requests, shadow=shadow)

    def note_tool_result(self, record, items):
        mode = self.mode()
        if mode:
            requests = requests_for_tool_record(record.subject_id, record, items)
            if requests:
                self.questions.put_signals(record.subject_id, requests, shadow=mode == 1)

    def note_revision(self, record, cognition):
        if record.content['to'] in EpistemicRevisedSource.target_states:
            mode = self.mode()
            if mode:
                request = EpistemicRevisedSource.from_record(record, cognition)
                self.questions.put_signals(record.subject_id, (request,), shadow=mode == 1)

    def revisit(self, subject, request_id, segment_id, *, stage='evidence', suggestion=None):
        if self.mode() != 2:
            raise ValueError("关联新证据需要启用内在活动模式 2")
        original = self.questions.lookup(subject, request_id)
        if original is None:
            raise ValueError("当前主体没有这道内在认知题")
        reader = getattr(self.process.activity_ledger, 'experiences_by_ids', None)
        rows = reader(subject, (segment_id,)) if callable(reader) else ()
        if not rows or rows[0].status not in (SegmentStatus.ACCEPTED, SegmentStatus.CONSUMED):
            raise ValueError("当前主体没有这段可用经历")
        segment = rows[0]
        if segment.output_kind not in (OutputKind.EXTERNAL_INPUT, OutputKind.SUBJECT_REPLY) or not segment.text_raw:
            raise ValueError("请指定有原文的交谈或实际回应")
        if segment_delivery(segment) == DELIVERY_PLANNED_AUDIO:
            raise ValueError("这段语音还没有实际播出的记录")
        person = original.object_id
        if stage in ('adopted', 'outcome'):
            source = self.questions.suggestion_source(subject, request_id)
            suggestion = self.questions.select_suggestion(source, stage, suggestion)
            cognition = self.process.repository.get_cognitive_content(source['cognition_id'])
            if segment.occurred_at.utcoffset() is None or segment.occurred_at < cognition.created_at:
                raise ValueError("采用或结果的原段必须发生在建议形成以后")
            if suggestion == 'interaction':
                person = source['suggestions'][suggestion]['object_id']
        if person:
            actor = segment.actor_object_id
            if segment.output_kind == OutputKind.SUBJECT_REPLY:
                previous = self.process.activity_ledger.read_experience_page(
                    subject, after_sequence=max(0, segment.sequence - 2), limit=1)
                actor = (previous[0].actor_object_id if previous and previous[0].sequence == segment.sequence - 1
                         and previous[0].output_kind == OutputKind.EXTERNAL_INPUT else None)
            if actor != person:
                raise ValueError("这段发言的对象与原题不一致")
        evidence = dict(original.evidence, segment_ids=[segment_id], source_versions=['segment:' + segment_id])
        request = replace(original, request_id=new_id(), evidence=evidence, entry_at_also=segment.occurred_at)
        self.questions.attach_followup(subject, request_id, request, segment, stage=stage, suggestion=suggestion)
        return self.questions.lookup(subject, request_id)

    def candidates(self, context):
        started = perf_counter()
        batch = self.assistant.discover(context)
        requests = tuple(batch.requests)
        if any(not isinstance(r, IntrospectionRequest) or r.subject_id != context.subject_id
               for r in requests):
            raise ValueError("discovery returned a question outside the requested subject")
        summary = {"source": context.source, "candidates": len(requests),
                   "discovery_ms": round((perf_counter() - started) * 1000, 1)}
        return requests, (*batch.coverage, summary)

    def scan_signals(self, subject, *, shadow=False, checkpoint=lambda: None):
        if not shadow:
            self._adopt_legacy(subject)
        key = 'signals'
        cursor = self.questions.cursor(subject, key, shadow=shadow)
        signals, end = self.questions.next_signals(subject, shadow=shadow, after=cursor)
        if not signals:
            return ()
        context = signal_context(subject, signals, hang_store=self.process.hang_store)
        requests, coverage = self.candidates(context)
        checkpoint()
        admitted = []
        self.questions.submit(subject, requests, shadow=shadow, cursor_key=key, accepted=admitted,
                              expected_cursor=cursor, cursor=end, coverage=coverage)
        return tuple(admitted)

    def scan(self, subject, *, shadow=False, object_id=None, checkpoint=lambda: None, now=None):
        """Read one bounded page per input source. An empty result can still advance coverage."""
        if not shadow:
            self._adopt_legacy(subject)
        accepted = []
        now = now or datetime.now(BEIJING)
        for source in ('ledger', 'tools'):
            checkpoint()
            key = source if object_id is None else f'{source}/person/{object_id}'
            cursor = self.questions.cursor(subject, key, shadow=shadow)
            due = (self.questions.due_segments(subject, key, shadow=shadow, now=now.timestamp())
                   if source == 'ledger' else ())
            context = read_discovery_context(self.process, subject, source, cursor, now=now,
                                             object_id=object_id, due_segment_ids=due)
            if context.next_cursor == cursor and not due:
                continue
            excluded = set()
            if context.segments and not shadow:
                excluded = self.questions.covered(subject, ['segment:' + s.segment_id for s in context.segments],
                                                  owner='legacy')
                # A neighboring fresh segment may still merit its own question.
                context = replace(context, segments=tuple(s for s in context.segments
                                                          if 'segment:' + s.segment_id not in excluded))
            requests, coverage = self.candidates(context)
            if excluded:
                coverage = (*coverage, {'source':'ledger', 'covered_by_legacy': sorted(excluded)})
            if object_id is not None and any(r.object_id != object_id for r in requests):
                raise ValueError("discovery returned a question outside the requested person")
            checkpoint()
            self.questions.submit(subject, requests, shadow=shadow, cursor_key=key, accepted=accepted,
                                  expected_cursor=cursor, cursor=context.next_cursor, coverage=coverage,
                                  deferred=context.deferred_segments, resolved=context.resolved_segments)
        return tuple(accepted)

    def recall_question(self, subject, task, materials, reason):
        """Only a visible original event can seed a deeper reinterpretation."""
        visible = set(materials['main_cognition'].get('model_visible_ids', ()))
        events = [event for event in materials['events']
                  if event.get('event_id') in visible and event.get('kind') != 'reflection_insight'
                  and not event['event_id'].startswith('insight:')
                  and str(event.get('content') or event.get('text') or '').strip()]
        if not events:
            return None
        ref = 'recall:' + task['run_id']
        evidence = {'reason': reason, 'recall_events': events,
                    'source_versions': [ref, *(e['event_id'] for e in events)]}
        request = IntrospectionRequest(
            subject_id=subject, trigger=IntrospectionTrigger.EVENT_RESULT, entry_ref=ref,
            entry_at=datetime.fromtimestamp(task['created'], timezone.utc), entry_text=reason,
            object_id=task.get('object_id') or None, level=IntrospectionLevel.MEDIUM,
            evidence=evidence, dedup_key=ref, origin='recall_reprocess')
        questions, _ = self.candidates(signal_context(subject, (request,)))
        if not questions:
            return None
        # One owner per observation. The evaluator's visible originals stay authoritative.
        return replace(request, entry_text=questions[0].entry_text)

    @contextmanager
    def internal_read(self):
        local = getattr(getattr(self.process, 'memory', None), '_reflection_local', None)
        if local is None:
            yield
            return
        previous = getattr(local, 'internal', False)
        local.internal = True
        try:
            yield
        finally:
            local.internal = previous

    def run(self, job, *, checkpoint=lambda: None):
        """Freeze inputs before the model call, then publish the saved answer idempotently."""
        request = job['request']
        try:
            checkpoint()
            frozen = job['frozen']
            if frozen is None:
                collected = job['input']
                if collected is None:
                    started = perf_counter()
                    with self.internal_read():
                        collected = self.reflection.collect_question(request)
                    collected = dict(collected, collect_ms=round((perf_counter() - started) * 1000, 1))
                    checkpoint()
                    if not self.questions.freeze_input(job, collected):
                        return None
                checkpoint()
                started = perf_counter()
                frozen = self.reflection.prepare_question(request, collected=collected)
                frozen = dict(frozen, coverage=dict(frozen.get('coverage', {}),
                    collect_ms=collected.get('collect_ms'), six_ms=round((perf_counter() - started) * 1000, 1)))
                checkpoint()
                if not self.questions.freeze(job, frozen):
                    return None
            checkpoint()
            run = self.reflection.publish_question(request, frozen, job_key=job['job_key'])
            if self.questions.complete(job, state=frozen['state'], proposals=frozen.get('proposals', ()),
                                       activity_id=run.activity_id, cognitive_content_id=run.cognitive_content_id):
                return run
            return None
        finally:
            self.questions.release(job)

    def drain(self, subject, *, limit=1, checkpoint=lambda: None):
        runs = []
        for _ in range(max(1, limit)):
            selected = subject if subject is not None else self.questions.next_subject()
            if selected is None:
                break
            job = self.questions.claim(selected)
            if job is None:
                break
            run = self.run(job, checkpoint=checkpoint)
            if run is None:
                break
            runs.append(run)
        return tuple(runs)

    def reflect(self, subject, prompt):
        # Two identical explicit commands are separate user actions.
        request_id = new_id()
        ref = 'manual:' + request_id
        request = IntrospectionRequest(
            subject_id=subject, trigger=IntrospectionTrigger.MANUAL, entry_ref=ref,
            entry_at=utc_now(), entry_text=(prompt or '').strip(), level=IntrospectionLevel.HIGH,
            urgency=IntrospectionUrgency.URGENT, origin='cli', request_id=request_id,
            evidence={'reason': '人工指定回看主题', 'source_versions': [ref]})
        job = self.questions.submit_and_claim(request)
        run = self.run(job)
        if run is None or not run.cognitive_content_id:
            raise RuntimeError("manual introspection did not commit its result")
        return self.process.repository.get_cognitive_content(run.cognitive_content_id)
