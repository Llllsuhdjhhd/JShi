"""Exercise the new recall owner through the real reflection scheduler with an offline model."""
import json
import time
from types import SimpleNamespace

import pytest

from jshi.core.param_overlay import override_snapshot
from jshi.memory.port import RecalledFragment
from jshi.models import ModelResponse
from jshi.reflection.executor import ReflectionExecutor
from jshi.reflection.memory_store import ReflectionMemoryStore
from test_introspection import IntrospectionModel, runtime


class RecallModel(IntrospectionModel):
    def __init__(self, wants_review):
        super().__init__(payload={
            'what': '我注意到对方停顿后还会接着说。', 'choice_and_result': '我等他把话说完。',
            'why': '上一轮也有相同的停顿。', 'better': '保留足够的等待时间。',
            'next_time': '先等对方说完。', 'tool_need': False, 'worth_learning': False,
            'candidates': [],
        })
        self.wants_review = wants_review
        self.evaluations = []
        self.six_questions = []

    def generate(self, request):
        if request.purpose == 'introspection':
            if request.input_text.startswith('{'):
                payload = json.loads(request.input_text)
                if 'events' in payload:
                    self.evaluations.append(payload)
                    assert '发现新关系' not in request.system_extra
                    answer = {'feedback': [{'event_id': 'E1', 'judgment': 'helpful',
                                            'reason': '这一轮也出现了相同的停顿。'}],
                              'needs_reprocess': self.wants_review,
                              'reprocess_reason': '这次停顿和之前有什么共同点？'}
                    return ModelResponse(text=json.dumps(answer, ensure_ascii=False), model=self.name)
            self.six_questions.append(request.input_text)
        return super().generate(request)


def observe(executor, *, insight=False):
    request = SimpleNamespace(subject_id='stone', activity_id='activity-original', input_revision=1,
                              run_id='recall-run-original', query_text='他停顿时应该怎么做？',
                              interlocutor_object_id='OBJ-USER', created_at=time.time(),
                              object_ids=('OBJ-USER',), mode=2, session_id='conversation', policy_version=1)
    original = RecalledFragment('insight:original' if insight else 'original-fragment', 'event',
                                '他停了几秒后，补完了刚才那句话。',
                                kind='reflection_insight' if insight else 'fact', object_id='OBJ-USER')
    omitted = RecalledFragment('unseen-fragment', 'event', '另一段未给主认知看的描述。')
    batch = SimpleNamespace(request=request, candidates=(original, omitted), query_runs=(), skipped=(),
                            invalidated_ids=(), reused_ids=(), elapsed_ms=1, truncated=False, status='ready')
    executor.store.observe_run(batch, (original,))
    executor.store.link_run_outcome(request.run_id, {}, {})
    assert executor.store.pending('stone')['deep_owner'] == 'discovery'


@pytest.mark.parametrize(('wants_review', 'insight', 'should_queue'),
                         [(True, False, True), (False, False, False), (True, True, False)])
def test_recall_owner_routes_only_selected_originals(tmp_path, wants_review, insight, should_queue):
    model = RecallModel(wants_review)
    with override_snapshot({'INNER_ACTIVITY_MODE': 2}):
        process, _ = runtime(tmp_path, model=model)
        executor = process.reflection_executor
        observe(executor, insight=insight)
        assert executor.tick('stone', force=True)
        assert executor.store.pending('stone') is None
        assert len(model.evaluations) == 1
        assert executor.store.questions.has_pending('stone') == should_queue
        if should_queue:
            assert executor.tick('stone', force=True)
            assert not executor.store.questions.has_pending('stone')
            assert len(model.six_questions) == 1
            assert 'M1' in model.six_questions[0]
            assert '补完了刚才那句话' in model.six_questions[0]
            assert '另一段未给主认知看的描述' not in model.six_questions[0]
            assert 'original-fragment' not in model.six_questions[0]
        else:
            assert not model.six_questions
        with executor.store.connect() as db:
            assert db.execute('SELECT count(*) FROM inner_runs').fetchone()[0] == int(should_queue)
            assert db.execute("SELECT count(*) FROM observations WHERE stage='done'").fetchone()[0] == 1


def test_committed_ratings_projection_recovers_after_restart(tmp_path, monkeypatch):
    model = RecallModel(False)
    with override_snapshot({'INNER_ACTIVITY_MODE': 2}):
        process, _ = runtime(tmp_path, model=model)
        executor = process.reflection_executor
        observe(executor)
        record = process.effectiveness.record_ratings

        def crash_after_write(*args):
            record(*args)
            raise RuntimeError('connection lost after recording ratings')

        with monkeypatch.context() as patch:
            patch.setattr(process.effectiveness, 'record_ratings', crash_after_write)
            assert not executor.tick('stone', force=True)
        assert executor.store.pending('stone') is None
        assert executor.store.pending_projection('stone') is not None
    with override_snapshot({'INNER_ACTIVITY_MODE': 0}):
        reopened = ReflectionMemoryStore(executor.store.path)
        replacement = ReflectionExecutor(process, process.reflection, reopened, model)
        assert replacement.tick('stone', force=True)
        assert reopened.pending_projection('stone') is None
        assert len(model.evaluations) == 1
        assert not model.six_questions
        rows = process.effectiveness.ratings.list('stone')
        assert len(rows) == 1 and rows[0].activity_id == 'activity-original'
