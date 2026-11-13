import json
import threading
from types import SimpleNamespace

import pytest

from jshi.tool import FeedbackKind, HangStore, ToolFeedback, ToolModule, ToolRequest, ToolResult, ToolRunner, ToolService, ToolStatus
from jshi.tool.pi_engine import PiEngine
from jshi.tool.plan import PlanFailure
from jshi.tool.supervision import Review, review_execution
from jshi.tool.verification import can_advance


class Model:
    def __init__(self, output):
        self.output = output
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return SimpleNamespace(text=json.dumps(self.output, ensure_ascii=False))


def review_json(decision='accept', evidence='实际内容'):
    return {'decision': decision, 'summary': '成果核对说明', 'instruction': '保留旧内容，补齐遗漏',
            'checks': [{'requirement': 1, 'status': 'met', 'evidence': evidence}]}


def test_reviewer_requires_real_evidence_and_gates_failed_physical_check():
    request = ToolRequest(need='实际内容', meta={'requirements': ['包含实际内容']})
    calls = [{'name': 'bash', 'output': '实际内容'}]
    assessment = {'verdict': 'unknown', 'completed_calls': 1}
    assert review_execution(Model(review_json()), request, calls, assessment).decision == 'accept'
    assert review_execution(Model(review_json(evidence='伪造')), request, calls, assessment).decision == 'blocked'
    assert review_execution(Model(review_json()), request, [], assessment, assistant='实际内容').decision == 'blocked'
    assert review_execution(Model(review_json()), request, calls, {**assessment, 'verdict': 'failed'}).decision == 'blocked'
    assert review_execution(Model(review_json()), request, calls, {**assessment, 'call_errors': True}).decision == 'blocked'


def test_unfinished_progress_is_not_mistaken_for_final_success():
    output = review_json()
    output['checks'][0]['status'] = 'unknown'
    request = ToolRequest(need='完成后续步骤')
    model = Model(output)
    assert review_execution(model, request, [], {}, context={'phase': 'progress'}).decision == 'accept'
    assert review_execution(model, request, [], {}, context={'phase': 'final'}).decision == 'blocked'


def test_review_clarification_blocks_and_preserves_questions():
    output = review_json()
    output['waiting_for'] = ['需要逐字记录还是只记录摘要？']
    review = review_execution(Model(output), ToolRequest(need='记录'),
        [{'name': 'bash', 'output': '实际内容'}], {'completed_calls': 1})
    assert review.decision == 'blocked'
    assert review.waiting_for == ('需要逐字记录还是只记录摘要？',)
    assert review.waiting_for[0] in review.summary


class Pi(PiEngine):
    def __init__(self, path, blocks):
        super().__init__(home_dir=path)
        self.blocks = blocks
        self.sent = []
        self.spawn_count = self.closed = 0

    def _spawn(self):
        self.spawn_count += 1
        return SimpleNamespace(poll=lambda: None)

    def _send(self, proc, payload):
        self.sent.append(payload)

    def _close(self, proc):
        self.closed += 1

    def _iter_events(self, proc, **kwargs):
        for index, text in enumerate(self.blocks):
            assert len([p for p in self.sent if p['type']=='prompt']) == index + 1
            yield {'type': 'tool_execution_start', 'toolName': 'bash', 'toolCallId': str(index), 'args': {}}
            yield {'type': 'tool_execution_end', 'toolName': 'bash', 'toolCallId': str(index),
                   'result': {'content': [{'type': 'text', 'text': text}]}, 'isError': False}
            yield {'type': 'agent_settled'}


def test_intermediate_steering_and_final_correction_reuse_one_pi_session(tmp_path):
    engine = Pi(tmp_path, ['结果缺了一项', '所有项目已补齐'])
    phases = []

    def reviewer(req, calls, assessment, assistant, *, phase='final'):
        phases.append(phase)
        if phase == 'progress':
            return Review('correct', '目标路径不对，纠正', '按原路径保存，不覆盖旧内容')
        if len(phases) == 2:
            return Review('correct', '仍有遗漏', '补齐遗漏并回读')
        return Review('accept', '已按要求核对')

    engine.reviewer = reviewer
    feedback = engine.execute(ToolRequest(command='skill:test', need='完整任务'))
    assert phases == ['progress', 'final', 'final']
    assert engine.spawn_count == engine.closed == 1
    assert [p['type'] for p in engine.sent] == ['prompt', 'steer', 'prompt']
    finals = [f for f in feedback if f.kind is FeedbackKind.RESULT]
    assert len(finals) == 1 and finals[0].result.status == ToolStatus.OK
    assert can_advance(finals[0].result)
    assert not engine._running


def test_correction_budget_stops_without_premature_success(tmp_path):
    engine = Pi(tmp_path, ['第一轮实际输出', '第二轮实际输出', '第三轮实际输出'])
    def reviewer(*args, phase='final'):
        return Review('accept', '仍在执行') if phase == 'progress' else Review('correct', '仍缺内容', '继续补齐')
    engine.reviewer = reviewer
    result = engine.execute(ToolRequest(need='完整任务'))[-1].result
    assert result.status == ToolStatus.PARTIAL
    assert not can_advance(result)
    assert '上限' in result.summary
    assert len(engine.sent) == 3


def test_cancellation_during_review_sends_no_correction(tmp_path):
    engine = Pi(tmp_path, ['实际输出'])
    cancel = threading.Event()
    def reviewer(*args, **kwargs):
        cancel.set()
        return Review('correct', '待纠正', '补齐')
    engine.reviewer = reviewer
    result = list(engine.iter_execute(ToolRequest(need='完整任务'), cancel=cancel))[-1].result
    assert result.status == ToolStatus.ABORTED
    assert len(engine.sent) == 1 and engine.closed == 1


def test_execution_clarification_stops_before_next_pi_stage(tmp_path):
    engine = Pi(tmp_path, ['必须先明确记录范围'])
    engine.reviewer = lambda *a, **k: Review('blocked', '需要确认', waiting_for=('记录谁的发言？',))
    result = engine.execute(ToolRequest(need='完整任务'))[-1].result
    assert result.status == ToolStatus.PARTIAL
    assert len(engine.sent) == 1 and engine.closed == 1


class Engine:
    name = 'stub'
    def __init__(self):
        self.calls = []
        self.guidance = []
        self.reviewer = None
    def execute(self, request):
        self.calls.append(request)
        return (ToolFeedback(request_id=request.request_id, kind=FeedbackKind.RESULT,
            result=ToolResult(status=ToolStatus.OK, summary='已执行')),)
    def steer(self, request_id, message):
        self.guidance.append((request_id, message))
        return True


def runtime(tmp_path, planner=None, wrap=None):
    store = HangStore(tmp_path/'hang.jsonl')
    engine = Engine()
    return ToolService(store, ToolRunner(ToolModule(engine), store), planner=planner, wrap_skill=wrap), engine


def test_two_rounds_of_clarification_keep_original_goal_and_owner(tmp_path):
    class Planner:
        def __init__(self): self.materials = []
        def plan(self, intake):
            self.materials.append(intake)
            if len(self.materials) == 1:
                return PlanFailure('范围待确认', ('记录谁的发言？',))
            if len(self.materials) == 2:
                return PlanFailure('交付待确认', ('逐字记录还是摘要？',))
            return ToolRequest(command='record', need=intake.need,
                meta={'requirements': ['仅记录委托人', '逐字保留', '说停才结束']})
    planner = Planner()
    service, engine = runtime(tmp_path, planner)
    first = service.intake(subject_id='s', object_id='o', need='记录对话', user_input='把谈话记录下来，直到我说停')
    service.drain_for_tests()
    work_id = service.intake_store.get(first.intake_id).meta['work_id']
    assert not engine.calls
    assert service.claim_notices('s', 'o')
    foreign = service.intake(subject_id='s', object_id='other', work_id=work_id, need='全部人都记')
    assert foreign.status == 'failed'
    service.intake(subject_id='s', object_id='o', work_id=work_id, need='记录我的发言直到说停', user_input='只记我的')
    service.drain_for_tests()
    assert not engine.calls
    service.intake(subject_id='s', object_id='o', work_id=work_id,
                   need='逐字记录我的发言直到我说停', user_input='逐字记录')
    service.drain_for_tests()
    assert len(engine.calls) == 1
    request = engine.calls[0]
    assert request.meta['user_input'] == '把谈话记录下来，直到我说停'
    assert request.meta['amendments'] == ['只记我的', '逐字记录']
    terms = json.loads(planner.materials[-1].meta['work_terms'])
    assert len(terms['clarifications']) == 2
    assert terms['original_request'] == request.meta['user_input']
    assert not service.work_index.get(work_id)['waiting_for']


def test_active_work_guidance_is_not_lost_as_duplicate(tmp_path):
    service, engine = runtime(tmp_path)
    service.work_index.create('w', 's', 'o', '原始完整目标')
    hang = service.hang_store.create(subject_id='s', object_id='o', need='原始完整目标', command='task')
    service._session_tasks.add(hang.task_id)
    service.work_index.bind('w', hang.task_id)
    record = service.intake(subject_id='s', object_id='o', work_id='w', need='补充', user_input='保留文件原有内容')
    assert record.meta['guidance_delivery'] == 'sent'
    assert len(engine.guidance) == 1
    assert service.work_index.get('w')['amendments'][0]['text'] == '保留文件原有内容'


def test_review_revision_race_reopens_checks_and_records_clarification(tmp_path):
    class Wrap:
        def review_execution(self, *a):
            service.work_index.amend('w', '增加新要求', 'new')
            return Review('accept', '旧目标已满足')
    service, engine = runtime(tmp_path, wrap=Wrap())
    service.work_index.create('w', 's', 'o', '目标')
    request = ToolRequest(need='目标', meta={'work_id': 'w'})
    result = engine.reviewer(request, [], {}, '')
    assert result.decision == 'correct' and '增加新要求' in result.instruction
    assert service.work_index.get('w')['reviews'][-1]['decision'] == 'correct'


def test_same_need_dedupe_also_preserves_user_guidance(tmp_path):
    service, engine = runtime(tmp_path)
    service.work_index.create('w', 's', 'o', '记录文件')
    hang = service.hang_store.create(subject_id='s', object_id='o', need='记录文件', command='task')
    service._session_tasks.add(hang.task_id)
    service.work_index.bind('w', hang.task_id)
    service.intake(subject_id='s', object_id='o', need='记录文件', user_input='后面的也要记')
    assert service.work_index.get('w')['amendments'][0]['text'] == '后面的也要记'
    assert engine.guidance and not engine.calls


def test_amendment_during_planning_replans_before_any_effect(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Planner:
        def __init__(self): self.calls = []
        def plan(self, intake):
            self.calls.append(intake)
            if len(self.calls) == 1:
                entered.set()
                assert release.wait(3)
                return ToolRequest(command='write', params={'path': 'old'})
            terms = json.loads(intake.meta['work_terms'])
            assert terms['amendments'][-1]['text'] == '改成新路径'
            return ToolRequest(command='write', params={'path': 'new'})
    planner = Planner()
    service, engine = runtime(tmp_path, planner)
    service.work_index.create('w', 's', 'o', '保存内容')
    service.intake(subject_id='s', object_id='o', work_id='w', need='保存内容', user_input='开始保存')
    assert entered.wait(3)
    try:
        result = service.intake(subject_id='s', object_id='o', work_id='w', need='保存内容', user_input='改成新路径')
        assert result.meta['dedupe'] == 'planning_work'
    finally:
        release.set()
    service.drain_for_tests()
    assert len(planner.calls) == 2
    assert [r.params['path'] for r in engine.calls] == ['new']


def test_review_timeout_retains_failure_reason_without_claiming_running():
    class TimeoutModel:
        def generate(self, request):
            raise TimeoutError('private provider details')
    result = review_execution(TimeoutModel(), ToolRequest(need='query'), [], {})
    assert result.decision == 'blocked'
    assert '超时' in result.summary and '执行已结束' in result.summary
    assert 'private' not in result.summary


def test_mixed_shell_receipt_is_verified_but_wrong_hash_still_fails(tmp_path):
    import hashlib
    from jshi.tool.verification import assess_pi
    target = tmp_path / 'result.json'
    target.write_text('{"opponent":"example"}', encoding='utf-8')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    def check(hash_value):
        output = 'query completed\n' + json.dumps({'artifacts': [{'path': str(target), 'sha256': hash_value}]})
        return assess_pi([{'name': 'bash', 'output': output}], base=tmp_path)
    assert check(digest)['verdict'] == 'passed'
    assert check('0' * 64)['verdict'] == 'failed'


def test_followup_material_distinguishes_stopped_work_and_keeps_evidence():
    from jshi.tool.service import format_tool_related
    text = format_tool_related([{'id': 'task', 'section': 'recent', 'status': '部分完成',
        'execution_state': '本次执行已结束', 'execution_excerpt': '查询返回了候选赛程',
        'end_reason': '监督模型响应超时', 'work_open': True, 'work_ids': ['work']}],
        task_codes={'task':'T1'}, work_codes={'work':'W1'})
    assert '执行已结束' in text and '不表示执行仍在运行' in text
    assert 'refresh_reason' in text and 'W1' in text and '超时' in text
    assert '查询返回了候选赛程' in text and '不代表完成' in text


def test_provider_timeout_category_is_preserved():
    from jshi.models.routing import ProviderTimeout
    class Failing:
        def generate(self, request):
            raise ProviderTimeout(7, 'tool', 'response_body')
    result = review_execution(Failing(), ToolRequest(need='query'), [], {})
    assert '响应超时' in result.summary


def test_nontext_receipt_does_not_crash_or_claim_verification(tmp_path):
    from jshi.tool.verification import assess_pi
    assert assess_pi([{'name': 'bash', 'output': {'artifacts': []}}], base=tmp_path)['verdict'] == 'unknown'
