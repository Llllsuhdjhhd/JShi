"""通用自省执行器：独立线程、共同排程与记录，可注册不同思考方式。"""
from __future__ import annotations

import json
import logging
import threading
import time

from jshi.core.params import introspection_enabled, reflection_param as param
from jshi.models import ModelRequest
from jshi.skill.base import SkillError
from .memory_store import dump
from .thoughts import RecallEvaluationThought, RecallReprocessingThought, PressureComparisonThought

logger = logging.getLogger(__name__)


class YieldToConversation(Exception):
    pass


class ReflectionExecutor:
    def __init__(self, process, legacy, store, model):
        self.process, self.legacy, self.store = process, legacy, store
        self.model = model
        self.skills = {
            'evaluate': RecallEvaluationThought(model),
            'reprocess': RecallReprocessingThought(model),
            'compare': PressureComparisonThought(model),
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
                self.handlers[generic['mode']](subject_id, json.loads(generic['payload']))
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
        result = self.skills[mode].run(request)
        # 已开始的模型请求无法强行取消；新输入到来则不继续后续处理。
        self.checkpoint()
        return result

    def materials(self, subject, task):
        items = json.loads(task['payload'])
        # 内部认识不能作为回忆再加工的种子，阻止递归复制。
        items = [item for item in items if item.get('kind') != 'reflection_insight']
        selected = []
        size = 0
        budget = int(param('REFLECTION_CONTEXT_CHARS')) // 2
        for item in items:
            length = len(dump(item))
            if size + length > budget:
                break
            selected.append(item)
            size += length
        from jshi.reflection.scene import segment_text
        segments = self.process.activity_ledger.list_experiences(subject)
        after = [segment_text(s) for s in segments
                 if s.occurred_at.timestamp() >= task['created']]
        outcome = '\n'.join(after)[:budget // 2]
        return {'question': task['query'], 'events': selected, 'subsequent_exchange': outcome,
                'main_cognition': json.loads(task.get('outcome', '{}')),
                'note': '返回的候选不等于主认知真正使用；缺少结果证据时只评价相关性。'}

    def evaluate(self, subject, task):
        materials = self.materials(subject, task)
        answer = self.ask('evaluate', subject, materials)
        visible = {item['event_id'] for item in materials['events']}
        self.store.save_feedback(task, [r for r in answer['feedback'] if isinstance(r, dict)], visible)
        self.store.finish_stage(task['id'], 'reprocess')
        self.audit(subject, 'recall_evaluation', {'observation': task['id'], 'visible_ids': sorted(visible), **answer})

    def reprocess(self, subject, task):
        materials = self.materials(subject, task)
        if not materials['events']:
            self.store.finish_stage(task['id'], 'done')
            return
        answer = self.ask('reprocess', subject, materials)
        ids = self.store.add_candidates(subject, [r for r in answer['candidates'] if isinstance(r, dict)],
                                       {r['event_id'] for r in materials['events']}, task['object_id'])
        self.store.set_candidates(task['id'], ids)
        self.audit(subject, 'recall_reprocessing', {'observation': task['id'], 'candidate_ids': ids, **answer})

    def compare(self, subject, task):
        rows = self.store.samples(subject)
        new_ids = set(json.loads(task.get('candidate_ids', '[]')))
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
        due = self.store.standard_due(subject)
        payload = {'samples': [{'bucket': r['bucket'], **r['item']} for r in sample],
                   'previous_standard': previous['text'] if previous else '', 'update_standard': due,
                   'capacity_per_bucket': int(param('REFLECTION_PRESSURE_BYTES'))}
        while len(dump(payload)) > int(param('REFLECTION_CONTEXT_CHARS')) and len(sample) > 1:
            sample.pop()
            payload['samples'].pop()
        answer = self.ask('compare', subject, payload)
        visible = {r['id'] for r in sample}
        ordered = answer['ordered_ids']
        reasons = answer['reasons']
        if (len(ordered) != len(visible) or set(ordered) != visible or
            any(not isinstance(r, dict) for r in reasons) or
            {r.get('id') for r in reasons} != visible or
            any(not str(r.get('reason', '')).strip() for r in reasons) or
            not set(answer['admit_ids']) <= visible or (due and not answer['standard'].strip()) or
            len(answer['standard']) > int(param('REFLECTION_STANDARD_CHARS'))):
            raise SkillError('comparison IDs/reasons do not match visible sample')
        self.store.order_and_trim(subject, ordered)
        self.store.advance_comparison(subject, max(1, len(sample) // 2))
        due = due and self.store.standard_due(subject)
        if due:
            self.store.save_standard(subject, answer['standard'], {'ordered_ids': ordered, 'reasons': reasons,
                                      'previous_version': previous['id'] if previous else None})
        size_fn = getattr(self.process.memory, 'memory_bytes', None)
        base_bytes = size_fn(subject) if callable(size_fn) else 0
        admitted = self.store.admit(subject, answer['admit_ids'], base_bytes)
        if task.get('id'):
            self.store.finish_stage(task['id'], 'done')
        self.audit(subject, 'pressure_comparison', {'sample_ids': ordered, 'standard_updated': due,
                                                  'admitted_ids': admitted, **answer})

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
