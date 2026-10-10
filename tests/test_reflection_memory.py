"""验证回忆反馈真正改变检索、压力容器更新及独立执行闭环。"""
import json
import time
from types import SimpleNamespace

import pytest

from jshi.core import params
from jshi.core.contracts import SubjectState, Provenance
from jshi.memory.shell import MemoryShell
from jshi.memory.port import RecalledFragment
from jshi.models import ModelResponse
from jshi.reflection.executor import ReflectionExecutor
from jshi.reflection.memory_store import ReflectionMemoryStore, dump
from jshi.subject import SubjectRepository


class Backend:
    def __init__(self):
        self.items = [RecalledFragment(str(n), 'event', f'waiting condition example {n}',
                      content=f'waiting condition example {n}', object_id='person', score=100-n)
                      for n in range(40)]
        self.calls = []

    def recall(self, subject, query, **kwargs):
        self.calls.append(kwargs)
        selected = list(self.items)
        anchors = kwargs.get('anchor_event_ids', ())
        selected.sort(key=lambda r: r.event_id not in anchors)
        return selected[:kwargs.get('limit') or len(selected)]

    def memory_bytes(self, subject):
        return 1000000


class ThoughtsModel:
    name = 'offline-thoughts'
    after_call = None
    bad = False

    def generate(self, request):
        data = json.loads(request.input_text)
        if self.bad:
            return ModelResponse(text='not JSON', model=self.name)
        if 'samples' in data:
            ids = [r['id'] for r in data['samples']]
            result = {'ordered_ids': ids[::-1], 'standard': '保留有证据、能够解释等待条件的认识。',
                      'admit_ids': ids, 'reasons': [{'id': i, 'reason': '有具体条件与依据'} for i in ids]}
        elif '发现新关系' in request.system_extra:
            result = {'candidates': [{'content': 'waiting condition：我认为等待应结合完整表达判断。',
                                     'source_ids': [data['events'][0]['event_id']], 'relation': '相似情境比较'}]}
        else:
            result = {'feedback': [{'event_id': data['events'][-1]['event_id'],
                                   'judgment': 'helpful', 'reason': '保留了具体等待条件'}]}
        if self.after_call:
            self.after_call()
        return ModelResponse(text=dump(result), model=self.name)


@pytest.fixture
def system(tmp_path):
    store = ReflectionMemoryStore(tmp_path / 'reflection.sqlite3')
    backend = Backend()
    memory = MemoryShell(backend)
    memory.reflection_store = store
    repo = SubjectRepository(tmp_path / 'subject.sqlite3')
    model = ThoughtsModel()
    legacy = SimpleNamespace(_subject_state=lambda subject: SubjectState(
        subject_id=subject, identity_summary='', current_stance='', provenance=Provenance(source='test')))
    process = SimpleNamespace(memory=memory, repository=repo,
                              activity_ledger=SimpleNamespace(list_experiences=lambda subject: ()),
                              activity_close=SimpleNamespace(close=lambda *a, **k: None))
    executor = ReflectionExecutor(process, legacy, store, model)
    return store, backend, memory, executor, model


def seed(store, text, refs=('source',)):
    return store.add_candidates('stone', [{'content': text, 'source_ids': list(refs), 'relation': '联系'}],
                                set(refs), 'person')


def test_feedback_changes_recall_and_reaches_previously_missing_event(system):
    store, backend, memory, _, _ = system
    original = memory.recall('stone', 'waiting condition', object_id='person', limit=5)
    observation = store.pending('stone')
    store.save_feedback(observation, [{'event_id': '35', 'judgment': 'helpful', 'reason': '具体条件'}], {'35'})
    improved = memory.recall('stone', 'waiting condition', object_id='person', limit=5)
    assert '35' not in [r.event_id for r in original]
    assert improved[0].event_id == '35'
    assert backend.calls[-1]['limit'] == 20
    assert '35' in backend.calls[-1]['anchor_event_ids']
    assert improved[0].score == backend.items[35].score


def test_feedback_is_scoped_and_cannot_rate_unseen_ids(system):
    store, _, _, _, _ = system
    store.observe('stone', 'waiting condition', 'person', [RecalledFragment('1', 'event', 'example')])
    observation = store.pending('stone')
    store.save_feedback(observation, [{'event_id': 'ghost', 'judgment': 'helpful', 'reason': '无依据'}], {'1'})
    assert store.adjustments('stone', 'waiting condition', 'person') == {}
    store.save_feedback(observation, [{'event_id': '1', 'judgment': 'helpful', 'reason': '有依据'}], {'1'})
    assert store.adjustments('stone', 'other subject entirely', 'person') == {}
    assert store.adjustments('stone', 'waiting condition', 'different-person') == {}
    assert store.adjustments('another-subject', 'waiting condition', 'person') == {}


def test_repeated_feedback_does_not_grow_without_bound(system):
    store, _, _, _, _ = system
    for _ in range(5):
        store.observe('stone', 'waiting condition', 'person', [RecalledFragment('1', 'event', 'example')])
        with store.connect() as db:
            observation = dict(db.execute('SELECT * FROM observations ORDER BY id DESC LIMIT 1').fetchone())
        store.save_feedback(observation, [{'event_id': '1', 'judgment': 'helpful', 'reason': '重复判断'}], {'1'})
    assert 0 < store.adjustments('stone', 'waiting condition', 'person')['1'] <= .35


def test_full_execution_persists_and_returns_new_memory(system):
    store, _, memory, executor, _ = system
    memory.recall('stone', 'waiting condition', object_id='person', limit=6)
    assert executor.tick('stone', force=True)  # 评价
    assert store.pending('stone')['stage'] == 'reprocess'
    assert executor.tick('stone', force=True)  # 新认识
    assert store.pending('stone')['stage'] == 'compare'
    assert executor.tick('stone', force=True)  # 压力比较与宽松入库
    assert store.pending('stone') is None
    result = memory.recall('stone', 'waiting condition', object_id='person', limit=6)
    insights = [r for r in result if r.kind == 'reflection_insight']
    assert len(insights) == 1
    assert insights[0].source_ids
    reopened = ReflectionMemoryStore(store.path)
    assert reopened.recall_insights('stone', 'waiting condition', 'person', 5)


def test_invalid_model_retains_task_for_retry(system):
    store, _, memory, executor, model = system
    memory.recall('stone', 'waiting condition', object_id='person', limit=6)
    model.bad = True
    assert not executor.tick('stone', force=True)
    assert store.pending('stone')['stage'] == 'evaluate'
    assert store.adjustments('stone', 'waiting condition', 'person') == {}


def test_conversation_preempts_and_does_not_commit_model_result(system):
    store, _, memory, executor, model = system
    memory.recall('stone', 'waiting condition', object_id='person', limit=6)
    calls = []
    busy = [True]
    assert not executor.tick('stone', force=True, busy=lambda: busy[0])
    busy[0] = False
    model.after_call = lambda: busy.__setitem__(0, True)
    assert not executor.tick('stone', force=True, busy=lambda: busy[0])
    assert store.pending('stone')['stage'] == 'evaluate'
    assert not store.adjustments('stone', 'waiting condition', 'person')


def test_timer_and_idle_gate(system):
    store, _, memory, executor, _ = system
    memory.recall('stone', 'waiting condition', object_id='person', limit=6)
    now = time.time()
    assert not executor.tick('stone', now=now)
    assert executor.tick('stone', now=now + 40)
    assert executor.tick('stone', now=now + 41)  # 已启动的流水线在后续闲时继续，不再等十分钟。


def test_pressure_keeps_both_buckets_rolling_and_real_memory_independent(system, monkeypatch):
    store, _, _, _, _ = system
    monkeypatch.setitem(params.REFLECTION_DEFAULTS, 'REFLECTION_PRESSURE_BYTES', (600, 256, 10000, 'int'))
    original = seed(store, 'first insight with evidence')[0]
    store.admit('stone', [original], 1000000)
    for index in range(30):
        ids = seed(store, f'new insight {index} ' + 'evidence ' * 6)
        samples = store.samples('stone')
        order = ids + [r['id'] for r in samples if r['id'] not in ids]
        store.order_and_trim('stone', order)
    rows = store.samples('stone')
    assert {r['bucket'] for r in rows} == {'A', 'B'}
    assert all(sum(r['bytes'] for r in rows if r['bucket'] == b) <= 600 for b in ('A', 'B'))
    assert original not in {r['id'] for r in rows}
    assert store.recall_insights('stone', 'first insight', 'person', 3)


def test_renewal_counts_unique_surviving_bytes_and_crosses_one_third(system):
    store, _, _, _, _ = system
    for n in range(6):
        seed(store, f'old insight {n}')
    assert store.standard_due('stone')
    store.save_standard('stone', '旧标准', {'reason': '初次比较'})
    assert not store.standard_due('stone')
    for _ in range(10):
        seed(store, 'old insight 0')
    assert not store.standard_due('stone')
    seed(store, 'new insight 0')
    assert not store.standard_due('stone')
    for n in range(1, 4):
        seed(store, f'new insight {n}')
    assert store.standard_due('stone')


def test_bogus_sources_and_recursive_insights_do_not_form_memory(system):
    store, _, memory, executor, _ = system
    assert not store.add_candidates('stone', [{'content': 'invented', 'source_ids': ['ghost']}], {'real'}, 'person')
    store.observe('stone', 'waiting condition', 'person', [RecalledFragment(
        'insight:x', 'reflection_insight', 'waiting condition', kind='reflection_insight')])
    store.finish_stage(store.pending('stone')['id'], 'reprocess')
    assert executor.tick('stone', force=True)
    assert store.pending('stone') is None
    assert store.samples('stone') == []


def test_quota_bounds_actual_memory(system, monkeypatch):
    store, _, _, _, _ = system
    monkeypatch.setitem(params.REFLECTION_DEFAULTS, 'REFLECTION_MEMORY_BYTES', (400, 256, 10000, 'int'))
    ids = []
    for n in range(5):
        ids += seed(store, f'insight {n} evidence ' * 4)
    store.admit('stone', ids, 1000000)
    with store.connect() as db:
        assert db.execute('SELECT COALESCE(SUM(bytes),0) FROM insights').fetchone()[0] <= 400


def test_new_thought_reuses_persistent_scheduler(system):
    store, _, _, executor, _ = system
    seen = []
    executor.register('relationship_review', lambda subject, task: seen.append((subject, task)))
    ident = executor.enqueue('stone', 'relationship_review', {'question': '这次交流改变了什么'}, theme='relationship')
    assert executor.enqueue('stone', 'relationship_review', {'question': '重复'}, theme='relationship') == ident
    assert executor.tick('stone', force=True)
    assert seen == [('stone', {'question': '这次交流改变了什么'})]
    assert store.pending_thought('stone') is None


def test_actual_model_visibility_reinforces_only_presented_insights(system):
    store, _, _, _, _ = system
    ident = seed(store, 'waiting condition insight')[0]
    store.admit('stone', [ident], 1000000)
    store.link_outcome('stone', 0, ['insight:' + ident], 'activity', [{'text': '回应计划'}])
    with store.connect() as db:
        assert db.execute('SELECT last_used FROM insights WHERE id=?', (ident,)).fetchone()[0] > 0
    assert not store.recall_insights('stone', 'waiting condition', None, 10, allowed_objects=('other-person',))


def test_worker_can_restart_after_host_disconnect(system):
    _, _, _, executor, _ = system
    first = executor.start('stone', lambda: True)
    old_worker, old_stop = executor._thread, executor._stop
    executor.detach(first)
    second = executor.start('stone', lambda: True)
    try:
        assert old_stop.is_set()
        assert executor._thread is not old_worker
        assert not executor._stop.is_set()
        old_worker.join(timeout=1)
        assert not old_worker.is_alive()
        assert executor._thread.is_alive()
    finally:
        executor.detach(second)
        executor._thread.join(timeout=1)
