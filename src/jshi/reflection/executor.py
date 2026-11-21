"""通用自省执行器：独立线程、共同排程与记录，可注册不同思考方式。"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from dataclasses import replace

from jshi.core.params import introspection_enabled, reflection_param as param
from jshi.models import ModelRequest
from jshi.skill.base import SkillError
from .memory_store import dump, similarity
from .thoughts import RecallEvaluationThought, RecallReprocessingThought
from .result_evaluation import ReflectionResultEvaluator

logger = logging.getLogger(__name__)


class YieldToConversation(Exception):
    pass


class ReflectionExecutor:
    def __init__(self, process, legacy, store, model):
        self.process, self.legacy, self.store = process, legacy, store
        self.model = model
        self.result_evaluator = getattr(process, 'reflection_result_evaluator', None)
        if self.result_evaluator is None:
            self.result_evaluator = ReflectionResultEvaluator()
        self.skills = {
            'evaluate': RecallEvaluationThought(model),
            'reprocess': RecallReprocessingThought(model),
            'compare': self.result_evaluator.comparison_skill(model),
        }
        self.handlers = {'evaluate': self.evaluate, 'reprocess': self.reprocess, 'compare': self.compare,
                         'behavior': self.behavior}
        self._lock = threading.Lock()
        self._hosts = {}
        self._host_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_input = time.time()
        self._busy = lambda: False
        self._active_stop = None
        self._retry_after = {}
        if hasattr(self.legacy, 'should_yield'):
            self.legacy.should_yield = lambda: (self._active_stop or self._stop).is_set() or self._busy()

    def register(self, name, handler):
        """handler(subject_id, task)，重用同一执行、让路和留痕机制。"""
        self.handlers[name] = handler

    def enqueue(self, subject_id, mode, payload, *, theme=''):
        """后续自省类型共用持久队列，不另起一套执行线程。"""
        if mode not in self.handlers:
            raise ValueError('unknown reflection mode: ' + mode)
        if len(dump(payload)) > int(param('REFLECTION_CONTEXT_CHARS')):
            raise ValueError('thought payload exceeds material budget')
        return self.store.enqueue_thought(subject_id, mode, payload, theme or dump(payload))

    def note_input(self):
        self._last_input = time.time()

    def start(self, subject_id, busy):
        token = object()
        with self._host_lock:
            self._hosts[token] = (subject_id, busy)
            if self._stop.is_set() or self._thread is None or not self._thread.is_alive():
                self._stop = threading.Event()
                self._thread = threading.Thread(target=self._work, args=(self._stop,), name='jshi-introspection', daemon=True)
                self._thread.start()
        return token

    def detach(self, token):
        with self._host_lock:
            self._hosts.pop(token, None)
            if not self._hosts:
                self._stop.set()

    def checkpoint(self):
        if (self._active_stop or self._stop).is_set() or self._busy():
            raise YieldToConversation()

    def _work(self, stop):
        while not stop.wait(float(param('REFLECTION_POLL_SECONDS'))):
            with self._host_lock:
                hosts = list(self._hosts.values())
            for subject in dict.fromkeys(s for s, _ in hosts):
                callbacks = [fn for s, fn in hosts if s == subject]
                try:
                    self.tick(subject, busy=lambda: any(fn() for fn in callbacks))
                except Exception:
                    logger.exception('background reflection failed')

    def tick(self, subject_id, *, busy=lambda: False, now=None, force=False):
        now = time.time() if now is None else now
        if not introspection_enabled() or busy():
            return False
        pending = self.store.pending(subject_id)
        continuing = pending and pending['stage'] != 'evaluate'
        if not force and (now - self._last_input < float(param('REFLECTION_IDLE_SECONDS'))
                          or now < self._retry_after.get(subject_id, 0)
                          or (not continuing and now - self.store.last_run(subject_id) < float(param('REFLECTION_INTERVAL_SECONDS')))):
            return False
        if not self._lock.acquire(blocking=False):
            return False
        self._busy = busy
        self._active_stop = self._stop
        try:
            generic = self.store.pending_thought(subject_id)
            task = self.store.pending(subject_id)
            if generic:
                self.checkpoint()
                self.store.ran(subject_id, now)
                payload = json.loads(generic['payload'])
                if generic['mode'] == 'review_behavior_result':
                    self.compare(subject_id, dict(payload, job_id=generic['id'], activity_id=generic['theme']))
                    return True
                self.handlers[generic['mode']](subject_id, payload)
                self.checkpoint()
                self.store.finish_thought(generic['id'])
                self.audit(subject_id, generic['mode'], {'job_id': generic['id']})
                return True
            if task:
                mode = task['stage']
            elif self.store.standard_due(subject_id):
                mode, task = 'compare', {'candidate_ids': '[]'}
            else:
                mode, task = 'behavior', {}
            self.checkpoint()
            self.store.ran(subject_id, now)  # 失败也受定时间隔约束，避免不断重试。
            self.handlers[mode](subject_id, task)
            return True
        except YieldToConversation:
            self.audit(subject_id, 'yield', {'reason': 'conversation_priority'})
            return False
        except Exception as exc:
            self._retry_after[subject_id] = now + float(param('REFLECTION_INTERVAL_SECONDS'))
            self.audit(subject_id, 'failed', {'error': type(exc).__name__})
            logger.exception('reflection task failed; pending task retained')
            return False
        finally:
            self._busy = lambda: False
            self._active_stop = None
            self._lock.release()

    def ask(self, mode, subject, payload):
        self.checkpoint()
        text = dump(payload)
        if len(text) > int(param('REFLECTION_CONTEXT_CHARS')):
            raise SkillError('reflection context exceeds budget')
        state_fn = getattr(self.legacy, '_subject_state', None)
        if callable(state_fn):
            state = state_fn(subject)
        else:
            from jshi.core.contracts import SubjectState, Provenance
            state = SubjectState(subject_id=subject, identity_summary='', current_stance='',
                                 provenance=Provenance(source='introspection'))
        request = ModelRequest(purpose='introspection', input_text=text, subject_state=state)
        record = getattr(self.process, '_record_step_input', None)
        if callable(record):
            record(replace(request, system_extra=self.skills[mode].system_extra(request)), self.model,
                   subject_id=subject, activity_id=getattr(self, '_record_activity_id', '') or 'reflection-' + str(time.time_ns()))
        result = self.skills[mode].run(request)
        # 已开始的模型请求无法强行取消；新输入到来则不继续后续处理。
        self.checkpoint()
        return result

    def materials(self, subject, task, *, reprocess=False):
        items = json.loads(task['payload'])
        # 内部认识不能作为回忆再加工的种子，阻止递归复制。
        if reprocess:
            items = [item for item in items if item.get('kind') != 'reflection_insight']
        selected = []
        size = 0
        budget = int(param('REFLECTION_CONTEXT_CHARS')) // 2
        for item in items:
            length = len(dump(item))
            if size + length > budget:
                continue
            selected.append(item)
            size += length
        from jshi.reflection.scene import segment_text
        segments = self.process.activity_ledger.list_experiences(subject)
        scope = set(json.loads(task.get('scope', '[]')))
        after = [segment_text(s) for s in segments
                 if task['created'] <= s.occurred_at.timestamp() <= task['created'] + float(param('RECALL_EVALUATION_WINDOW_SECONDS'))
                 and (not task.get('run_id') or task.get('activity_id') in s.source_ids
                      or (s.actor_object_id in scope and similarity(task['query'], s.text_raw or '') >= 0.2))]
        outcome = '\n'.join(after)[:budget // 2]
        return {'question': task['query'], 'events': selected, 'subsequent_exchange': outcome,
                'main_cognition': json.loads(task.get('outcome', '{}')),
                'note': '返回的候选不等于主认知真正使用；缺少结果证据时只评价相关性。'}

    def evaluate(self, subject, task):
        self._record_activity_id = task.get('activity_id') or 'reflection-' + str(task['id'])
        materials = self.materials(subject, task)
        payload, references = self.compact_materials(materials) if task.get('run_id') else (materials, {})
        answer = self.ask('evaluate', subject, payload)
        if task.get('run_id'):
            answer['feedback'] = [dict(item, event_id=references[item['event_id']])
                                  for item in answer['feedback'] if isinstance(item, dict)
                                  and item.get('event_id') in references]
        visible = {item['event_id'] for item in materials['events']}
        self.store.save_feedback(task, [r for r in answer['feedback'] if isinstance(r, dict)], visible)
        self.store.save_evaluation(task, answer, visible)
        reprocess = (answer.get('needs_reprocess') is True and bool(str(answer.get('reprocess_reason', '')).strip())
                     and bool(materials['events'])) if task.get('run_id') else True
        self.store.finish_stage(task['id'], 'reprocess' if reprocess else 'done')
        if task.get('run_id'):
            self.export_ratings(subject, task, answer, visible)
        self.audit(subject, 'recall_evaluation', {'observation': task['id'], 'visible_ids': sorted(visible), **answer})

    @staticmethod
    def compact_materials(materials):
        """Only semantic evidence and request-local references reach the evaluator."""
        references = {f'E{i}': item['event_id'] for i, item in enumerate(materials['events'], 1)}
        reverse = {value: key for key, value in references.items()}
        events = []
        actors = {}
        for code, item in zip(references, materials['events']):
            stamp = item.get('occurred_at')
            if stamp:
                try:
                    when = datetime.fromisoformat(str(stamp))
                    stamp = when.strftime('%Y-%m-%d %H:%M %z')
                except ValueError:
                    stamp = None
            actor_ids = tuple(dict.fromkeys((
                *((item['object_id'],) if item.get('object_id') else ()),
                *(item.get('interlocutor_object_ids') or ()),
            )))
            for ident in actor_ids:
                actors.setdefault(ident, f'P{len(actors) + 1}')
            events.append({'event_id': code, 'content': item.get('content') or item.get('text', ''),
                           'kind': item.get('kind', ''), 'occurred_at': stamp,
                           'actors': [actors[ident] for ident in actor_ids],
                           'query_object_role': item.get('query_object_role'),
                           'validity': item.get('validity', 'active')})
        outcome = materials['main_cognition']
        ratings = outcome.get('main_ratings') or {}
        compact_ratings = [dict(item, ref=reverse[item['ref']]) for item in ratings.get('items', ())
                           if item.get('ref') in reverse]
        payload = dict(materials, events=events, main_cognition={
            'visible': [reverse[ident] for ident in outcome.get('model_visible_ids', ()) if ident in reverse],
            'planned_response': outcome.get('planned_response', []),
            'response_status': outcome.get('response_status', 'unknown'),
            'delivery_status': outcome.get('delivery_status', 'unknown'),
            'usage': compact_ratings, 'coverage': ratings.get('coverage', 'unknown')})
        return payload, references

    def export_ratings(self, subject, task, answer, reviewed):
        """One per-activity projection for quality analysis, never an independent score."""
        from jshi.models import MemoryRating, MemoryRatings
        outcome = json.loads(task.get('outcome', '{}'))
        main = outcome.get('main_ratings') or {}
        visible = set(outcome.get('model_visible_ids', ()))
        ratings = {item['ref']: MemoryRating(**item) for item in main.get('items', ())
                   if item.get('ref') in visible}
        for item in answer.get('feedback', ()):
            ident = item.get('event_id')
            if ident not in visible or ident not in reviewed or ident in ratings:
                continue
            judgment = item.get('judgment')
            if judgment not in {'helpful', 'irrelevant', 'misleading'}:
                continue
            ratings[ident] = MemoryRating(ref=ident,
                relevance='related' if judgment == 'helpful' else 'unrelated' if judgment == 'irrelevant' else 'partial',
                misleading=judgment == 'misleading')
        coverage = main.get('coverage') or answer.get('coverage', '')
        if coverage == 'unknown':
            coverage = ''
        value = MemoryRatings(items=tuple(ratings.values()), coverage=coverage,
                              gap_query=main.get('gap_query') or answer.get('gap_query', ''))
        effectiveness = getattr(self.process, 'effectiveness', None)
        if effectiveness is not None and value.has_content():
            effectiveness.record_ratings(subject, task['activity_id'], value)

    def reprocess(self, subject, task):
        self._record_activity_id = task.get('activity_id') or 'reflection-' + str(task['id'])
        materials = self.materials(subject, task, reprocess=True)
        if not materials['events']:
            self.store.finish_stage(task['id'], 'done')
            return
        payload, references = self.compact_materials(materials) if task.get('run_id') else (materials, {})
        answer = self.ask('reprocess', subject, payload)
        if references:
            answer['candidates'] = [dict(item, source_ids=[references[ref] for ref in item.get('source_ids', ())])
                                    for item in answer['candidates'] if isinstance(item, dict)
                                    and item.get('source_ids') and all(ref in references for ref in item['source_ids'])]
        events = materials['events']
        candidates = self.result_evaluator.recall_proposals(answer['candidates'], events)
        # 查询对象可能只是被提及的人。相处经验要求所引事件能归到同一交往对象。
        actors, evidence = {}, {}
        for event in events:
            people = set(event.get('interlocutor_object_ids') or ())
            if not people and event.get('object_id') and event.get('query_object_role') == 'interlocutor':
                people.add(event['object_id'])
            actors[event['event_id']] = people
            evidence[event['event_id']] = event.get('content') or event.get('text', '')
        candidates = [dict(item) for item in candidates]
        for item in candidates:
            item.pop('person_scope', None)
            refs = item.get('source_ids')
            if isinstance(refs, list) and refs and all(isinstance(ref, str) for ref in refs):
                groups = [actors.get(ref, set()) for ref in refs]
                if len(groups[0]) == 1 and all(group == groups[0] for group in groups):
                    person = next(iter(groups[0]))
                    if not task['object_id'] or task['object_id'] == person:
                        item['person_scope'] = person
        items = self.store.prepare_candidates(candidates, set(evidence), task['object_id'], subject, evidence=evidence)
        ids = [item['id'] for item in items]
        self.store.set_proposals(task['id'], items)
        self.audit(subject, 'recall_reprocessing', {'observation': task['id'], 'candidate_ids': ids, **answer})

    def queue_behavior_result(self, run, scene):
        from .scene import segment_text
        references = {}
        for index, segment in enumerate(scene.segments, 1):
            references[f'E{index}'] = (segment.segment_id, segment_text(segment), segment.actor_object_id)
        for index, item in enumerate(scene.recalled, 1):
            if item.kind != 'reflection_insight' and not item.event_id.startswith('insight:'):
                people = item.interlocutor_object_ids
                person = people[0] if len(people) == 1 else None
                references[f'M{index}'] = (item.event_id, item.content or item.text, person)
        if not references:
            return
        source = run.cognitive_content_id
        evidence = {ident: text for ident, text, _ in references.values()}
        evidence[source] = run.answer.choice_and_result
        candidates = []
        for item in run.answer.candidates:
            codes = item.get('source_refs', [])
            if not isinstance(codes, list) or any(not isinstance(code, str) or code not in references for code in codes):
                continue
            cited = [references[code] for code in codes]
            people = {person for _, _, person in cited}
            candidates.append(dict(content=item.get('content'), relation=run.answer.why,
                source_ids=[ident for ident, _, _ in cited] or [source],
                person_scope=next(iter(people)) if len(people) == 1 else None))
        subject = run.request.subject_id
        candidates = self.result_evaluator.recall_proposals(candidates, [{'text': t} for t in evidence.values()])
        items = self.store.prepare_candidates(candidates, set(evidence), run.request.object_id, subject, evidence=evidence)
        if items:
            job = self.store.enqueue_thought(subject, 'review_behavior_result', {'proposals': items}, run.activity_id)
            if job is None:
                self.audit(subject, 'result_review_skipped', {'activity_id': run.activity_id, 'reason': 'queue_full'})

    def compare(self, subject, task):
        self._record_activity_id = task.get('activity_id') or ('reflection-' + str(task['id']) if task.get('id') else '')
        proposals = task.get('proposals', [])
        if isinstance(proposals, str):
            proposals = json.loads(proposals)
        rows = self.store.samples(subject)
        previous_ids = {row['id'] for row in rows}
        rows.extend({'id': item['id'], 'bucket': 'pending', 'item': item}
                    for item in proposals if item['id'] not in previous_ids)
        new_ids = ({item['id'] for item in proposals} if proposals
                   else set(json.loads(task.get('candidate_ids', '[]'))))
        if not rows:
            if task.get('id'):
                self.store.finish_stage(task['id'], 'done')
            return
        cap = int(param('REFLECTION_COMPARE_ITEMS'))
        # 新内容 + 两桶首尾 + 中间轮换；保证有竞争压力而不是只比较最前排。
        sample = [row for row in rows if row['id'] in new_ids][:cap // 2]
        rotation = self.store.comparison_position(subject) % len(rows)
        middle = rows[rotation:] + rows[:rotation]
        edges = []
        for bucket in ('A', 'B'):
            group = [r for r in rows if r['bucket'] == bucket]
            edges.extend(group[:1] + group[-1:])
        for row in edges + middle:
            if len(sample) >= cap:
                break
            if row not in sample:
                sample.append(row)
        previous = self.store.latest_standard(subject)
        due = self.store.standard_due(subject) or (previous is None and len(rows) >= 4)
        decision = self.result_evaluator.compare(sample, previous['text'] if previous else '', due,
                                                 lambda payload: self.ask('compare', subject, payload))
        visible = {row['id'] for row in sample}
        if not decision.ordered or not set(decision.ordered) <= visible or (proposals and not new_ids.intersection(decision.ordered)):
            raise SkillError('result review did not cover the pending sample')
        decision.validate(decision.ordered, update_standard=due)
        size_fn = getattr(self.process.memory, 'memory_bytes', None)
        base_bytes = size_fn(subject) if callable(size_fn) else 0
        self.checkpoint()
        committed = self.store.commit_review(subject, task, proposals, decision, base_bytes, update_standard=due)
        if committed is not None:
            admitted, updated, delivered = committed
            self.audit(subject, 'pressure_comparison', {
                'observation': task.get('id'), 'job_id': task.get('job_id'), 'origin_activity': task.get('activity_id'),
                'sample_ids': list(decision.ordered), 'ordered_ids': list(decision.ordered),
                'standard_updated': updated, 'standard': decision.standard, 'admitted_ids': admitted,
                'admit_ids': list(decision.admit), 'discard_ids': list(decision.discard),
                'outlets': decision.outlets, 'delivered_outlets': delivered, 'reasons': [{'id': key, 'reason': reason}
                                                        for key, reason in decision.reasons.items()]})

    def behavior(self, subject, task):
        self.checkpoint()
        self.legacy.enqueue_idle(subject)
        memory = self.process.memory
        local = getattr(memory, '_reflection_local', None)
        if local is not None:
            local.internal = True
        try:
            self.legacy.drain(subject, limit=1)
        finally:
            if local is not None:
                local.internal = False

    def audit(self, subject, mode, content):
        from jshi.subject.domain import Activity, ActivityKind, CognitiveContent, CognitiveKind, EpistemicStatus, EvidenceKind, HistoryKind, HistoryRecord
        activity = Activity(subject_id=subject, kind=ActivityKind.INTERNAL, trigger='reflection:' + mode)
        self.process.repository.add_activity(activity)
        cognition = CognitiveContent(subject_id=subject, activity_id=activity.id, kind=CognitiveKind.EVALUATION,
            content=dump(content), epistemic_status=EpistemicStatus.CONSIDERING,
            evidence_kind=EvidenceKind.COGNITIVE_REASONING,
            source_ids=tuple(content.get('visible_ids', content.get('sample_ids', []))),
            model=getattr(self.model, 'name', None))
        self.process.repository.add_cognitive_content(cognition)
        self.process.repository.add_history(HistoryRecord(subject_id=subject, kind=HistoryKind.SUBJECT,
            event_type='reflection_' + mode, content={'activity_id': activity.id, **content},
            source_ids=cognition.source_ids))
        self.process.activity_close.close(subject, activity.id, final_response_statuses=(), action_id='', reason='reflection_finished')
