from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from jshi.core.envelope import SpeakerEvidence
from jshi.tool.contract import FeedbackKind, ToolFeedback, ToolResult, ToolStatus
from jshi.tool.hang import HangRecord
from jshi.voice.jev_material import task_context, scene_task_context


def project(*, outcome=None, status='notified', handling=None, closed=False, meta=None):
    feedback = () if outcome is None else (ToolFeedback(
        request_id='request', kind=FeedbackKind.RESULT,
        result=ToolResult(status=ToolStatus(outcome), summary='结果')),)
    hang = HangRecord(task_id='long-task-id', request_id='request', subject_id='s',
        object_id='person', need='查询事项', status=status, summary='包装或进度摘要',
        feedback=feedback, delivered_at=datetime.now(timezone.utc), meta=meta or {})
    work = {'handling': {'long-task-id': handling or {}}, 'closed': closed,
            'delivery_evidence': '关闭说明，不代表已播'}
    seen = []
    def list_for(subject, owner):
        seen.append((subject, owner))
        return [hang]
    process = SimpleNamespace(tool_service=SimpleNamespace(
        hang_store=SimpleNamespace(list_for=list_for),
        work_index=SimpleNamespace(for_task=lambda _: [work])))
    speaker = SpeakerEvidence(track_id='track', object_id='person', method='user_selected')
    result = task_context(process, 's', [speaker], code_of=lambda _: 'P1')
    return result, process, seen


def test_completed_but_deferred_is_not_running_or_delivered():
    rows, _, _ = project(outcome='ok', handling={'disposition': 'deferred'})
    assert rows[0]['state'] == '执行已结束'
    assert rows[0]['result_available'] is True
    assert rows[0]['delivery'] == '暂存待交付'
    assert rows[0]['entered_main'] is True
    assert rows[0]['owner'] == 'P1'
    assert 'long-task-id' not in str(rows)


@pytest.mark.parametrize('outcome,available', [('ok', True), ('partial', True),
    ('failed', False), ('aborted', False), ('rejected', False), ('budget_exceeded', False)])
def test_terminal_result_keeps_actual_outcome(outcome, available):
    rows, _, _ = project(outcome=outcome)
    assert rows[0]['state'] == '执行已结束'
    assert rows[0]['result_available'] is available
    assert rows[0]['result'] != '尚无终态结果'


def test_summary_and_main_seen_do_not_prove_result_or_delivery():
    rows, _, _ = project(status='open', closed=True)
    assert rows[0]['state'] == '执行中'
    assert rows[0]['result_available'] is False
    assert rows[0]['delivery'] == '交付情况未核实'
    rows, _, _ = project(outcome='ok', handling={'disposition': 'answered', 'evidence': '结果已采用'})
    assert '实际送达须核对' in rows[0]['delivery']


def test_unresolved_person_and_invalidated_work_do_not_leak():
    _, process, seen = project()
    seen.clear()
    assert task_context(process, 's', [SpeakerEvidence(track_id='track', object_id='person', status='unknown')]) == []
    assert seen == []
    assert project(meta={'invalidated': 'true'})[0] == []
    assert project(meta={'invalidated': 'false'})[0]


def test_scene_tasks_follow_confirmed_selected_event_not_entire_codebook():
    _, process, seen = project(outcome='ok')
    seen.clear()
    codes = {'P1': 'person', 'P2': 'someone-else'}
    assert scene_task_context(process, 's', [{'p': 'P1', 'identity': {'certainty': 'tentative'}}], codes) == []
    rows = scene_task_context(process, 's', [{'p': 'P2', 'identity': {'person': 'P1', 'certainty': '确定'}}], codes)
    assert rows[0]['owner'] == 'P1'
    assert seen == [('s', 'person')]


def test_scene_writer_receives_task_state_with_same_clock(tmp_path):
    from jshi.models import ModelResponse
    from jshi.voice.jev_scene import JEVSceneStore, JEVSceneWriter
    from time import time
    import json
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('input', {'p': 'P1', 'text': '已经返回了吗', 'at': time()*1000,
                          'judgment': {'person': 'P1', 'certainty': '确定'}},
                 codes={'P1': 'person'}, labels={'person': 'lux'})
    requests = []
    def generate(request):
        requests.append(request)
        return ModelResponse(model='fake', text=json.dumps({'scene': '工具已结束，待核对交付。'}))
    _, process, _ = project(outcome='ok', handling={'disposition': 'deferred'})
    writer = JEVSceneWriter(SimpleNamespace(generate=generate))
    snapshot = store.snapshot()
    writer.run('s', snapshot, '', task_lookup=lambda events: scene_task_context(process, 's', events, snapshot['codes']))
    user = requests[0].input_text
    assert '【相关任务】' in user and '执行已结束' in user and '暂存待交付' in user
    assert 'long-task-id' not in user and 'notified' not in user
    data = json.loads(user.split('【相关任务】\n')[1].split('\n\n')[0])
    assert abs(data[0]['updated_at']) < 10  # Relative seconds, not epoch or milliseconds.
