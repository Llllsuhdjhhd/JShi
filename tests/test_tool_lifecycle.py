"""Local lifecycle scenarios: real files, no speech/provider calls."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from jshi.tool import ToolRequest, ToolFeedback, ToolResult, FeedbackKind
from jshi.tool.plan import PlanFailure
from jshi.tool.contract import utc_now
from tests.test_tool_work_reuse import runtime
from tests.test_voice import process, FakeCloud


def test_missing_information_resumes_same_work_after_restart(tmp_path):
    service, engine = runtime(tmp_path)
    due = (utc_now() + timedelta(hours=2)).isoformat()
    first = service.intake(subject_id='s', object_id='o', need='查明天天气', waiting_for=['城市'], expires_at=due)
    service.drain_for_tests()
    assert not engine.calls
    assert service.intake_store.get(first.intake_id).status == 'waiting'
    assert '城市' in service.format_tool_related_block('s', 'o')
    restored, engine = runtime(tmp_path)
    restored.planner = SimpleNamespace(plan=lambda r: ToolRequest(command='weather', params={'city':'杭州'}))
    result = restored.intake(subject_id='s', object_id='o', need='查杭州明天天气', work_id=first.intake_id)
    restored.drain_for_tests()
    assert len(engine.calls) == 1
    row = restored.work_index.get(first.intake_id)
    assert row['need'] == '查明天天气' and row['current_need'] == '查杭州明天天气'
    assert row['expires_at'] == due and not row['waiting_for']
    assert restored.intake_store.get(first.intake_id).status == 'resolved'
    assert any(r['work_id'] == first.intake_id for r in restored.work_index.for_task(restored.intake_store.get(result.intake_id).task_id))


def test_partial_answer_stays_waiting_and_other_owner_cannot_resume(tmp_path):
    service, engine = runtime(tmp_path)
    first = service.intake(subject_id='s', object_id='o', need='创建会议文件', waiting_for=['地点','内容范围'])
    second = service.intake(subject_id='s', object_id='o', need='桌面会议文件', work_id=first.intake_id, waiting_for=['内容范围'])
    other = service.intake(subject_id='s', object_id='other', need='桌面会议文件', work_id=first.intake_id)
    service.drain_for_tests()
    assert not engine.calls
    assert service.intake_store.get(second.intake_id).status == 'waiting'
    assert service.intake_store.get(other.intake_id).status == 'failed'
    assert service.work_index.get(first.intake_id)['waiting_for'] == ['内容范围']


def test_planner_missing_information_reports_once(tmp_path):
    service, engine = runtime(tmp_path)
    service.planner = SimpleNamespace(plan=lambda r: PlanFailure('我需要知道保存位置', ('保存位置',)))
    request = service.intake(subject_id='s', object_id='o', need='保存会议内容')
    service.drain_for_tests()
    assert not engine.calls
    assert service.claim_notices('s','o') == (request.intake_id,)
    assert service.claim_notices('s','o') == ()
    assert '待补信息' in service.format_tool_related_block('s','o')


@pytest.mark.parametrize('after_planning', [False, True])
def test_expired_request_never_dispatches_and_is_not_refreshed(tmp_path, after_planning):
    service, engine = runtime(tmp_path)
    calls = []
    def plan(record):
        calls.append(record)
        service.work_index.set_terms(record.meta['work_id'], expires_at=(utc_now()-timedelta(seconds=1)).isoformat())
        return ToolRequest(command='weather')
    service.planner = SimpleNamespace(plan=plan)
    request = service.intake(subject_id='s',object_id='o',need='原约定明天的天气',
                            expires_at=(utc_now()+timedelta(hours=1) if after_planning else utc_now()-timedelta(days=1)).isoformat())
    service.drain_for_tests()
    assert len(calls) == int(after_planning) and not engine.calls
    assert service.intake_store.get(request.intake_id).status == 'expired'
    assert '不再续办' in service.format_tool_related_block('s','o')
    service.intake(subject_id='s', object_id='o', need='恢复网络重试', work_id=request.intake_id,
                   expires_at=(utc_now()+timedelta(days=1)).isoformat())
    service.drain_for_tests()
    assert not engine.calls


def test_expiry_stops_active_work_once(tmp_path):
    service, _ = runtime(tmp_path)
    first = service.intake(subject_id='s',object_id='o',need='查当日天气',waiting_for=['城市'])
    service.work_index.set_terms(first.intake_id, expires_at=(utc_now()-timedelta(seconds=1)).isoformat())
    assert service.claim_notices('s','o') == (first.intake_id,)
    service.release_notices((first.intake_id,), handled=True)
    assert service.claim_notices('s','o') == ()


def test_execution_timeout_is_bounded_by_task_deadline(tmp_path):
    service, engine = runtime(tmp_path)
    service.planner = SimpleNamespace(plan=lambda r: ToolRequest(command='weather'))
    service.intake(subject_id='s',object_id='o',need='临时查询',expires_at=(utc_now()+timedelta(seconds=10)).isoformat())
    service.drain_for_tests()
    assert len(engine.calls)==1 and 0 < engine.calls[0].meta['timeout_s'] <= 10


def test_cancelled_work_cannot_be_reopened_by_old_input(tmp_path):
    service, engine = runtime(tmp_path)
    r=service.intake(subject_id='s',object_id='o',need='会议记录',waiting_for=['位置'])
    service.work_index.close(r.intake_id,'用户取消',cancelled=True)
    result=service.intake(subject_id='s',object_id='o',need='桌面',work_id=r.intake_id)
    service.drain_for_tests()
    assert result.status=='failed' and not engine.calls
    assert service.work_index.get(r.intake_id)['closed']


def test_new_missing_information_schema_parses_in_both_personas():
    from jshi.skill.cognition import _persona_to_model_response, _to_model_response
    for parse in (_persona_to_model_response, _to_model_response):
        value=parse({'mode':'respond','reply':'文件放在哪？','use_tool':True,'need':'记录会议',
                     'waiting_for':['文件位置'],'expires_at':'2026-11-12T23:59:00+08:00'},'fixture')
        assert value.tool_intent.waiting_for==('文件位置',)
        assert value.tool_intent.expires_at=='2026-11-12T23:59:00+08:00'


def test_resume_can_really_append_file_without_recreating_it(tmp_path):
    service, engine = runtime(tmp_path)
    path = tmp_path/'会议.txt'
    path.write_text('已保存的第一段\n', encoding='utf8')
    first = service.intake(subject_id='s',object_id='o',need='把会议内容追加到已有文件',waiting_for=['本次会议内容'])
    content = '今天先确定负责人与时间。'
    def execute(request):
        with path.open('a',encoding='utf8') as f: f.write(request.params['content']+'\n')
        from jshi.tool.verification import check_file
        check = check_file(str(path), base=tmp_path)
        assert check['passed']
        return (ToolFeedback(request_id=request.request_id,kind=FeedbackKind.RESULT,
            result=ToolResult(summary=str(path), result={'path':str(path),'content':path.read_text('utf8')},
                execution=({'kind':'verification','verdict':'passed','files':[check]},))),)
    engine.execute = execute
    service.planner = SimpleNamespace(plan=lambda r: ToolRequest(command='append',params={'content':content}))
    service.intake(subject_id='s',object_id='o',need='追加本次会议内容：'+content,work_id=first.intake_id)
    service.drain_for_tests()
    assert path.read_text('utf8') == '已保存的第一段\n'+content+'\n'


def test_displayed_reply_survives_failed_speech_without_becoming_heard(tmp_path):
    from jshi.voice.dialogue import VoiceDialogue
    d = VoiceDialogue(tmp_path/'dialogue.json')
    d.plan('s','k','session', ['文件已创建，等待开始。'])
    d.bind('k','r')
    assert not d.recent('s')
    d.displayed('r')
    d.stop('session')
    assert d.state['replies'][0]['played'] == []
    assert '未确认阅读或播出' in d.recent('s')[0]['basis']
    assert d.filter_text('文件已创建，等待开始。',subject='s') == '文件已创建，等待开始。'
    assert VoiceDialogue(d.path).recent('s')


@pytest.mark.parametrize('state', ['failed','stopped','completed'])
def test_short_ack_allowed_after_terminal_delivery(process, state):
    from jshi.app.voice import VoiceConversation
    from jshi.core.envelope import SceneUtterance, SpeakerEvidence
    from jshi.voice.jev import VoiceJEV
    from tests.test_jev_split_modes import Model, entry, preparation
    async def run():
        messages=[]
        async def send(m):messages.append(m)
        model=Model([entry(front=preparation(level=1,waiting_reply='好，我这就办。'))])
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(model,mode='split',fallback_models=[]))
        try:
            c.delivery.snapshot=lambda:{'state':state}
            speaker=SpeakerEvidence('typed',label='lux',object_id='lux-id',method='user_selected',status='recognized')
            utterance=SceneUtterance('给我桌面生成一个txt，记录这次会议',speaker,0,0,input_id='actual-create')
            decision, record, _ = await c._judge((utterance,),speaker,False)
            assert record['waiting_reply']=='好，我这就办。', record
            assert len(model.requests)==1
        finally:await c.close()
    asyncio.run(run())


def test_invalid_gate_does_not_drop_explicit_text_in_voice_page(process):
    from jshi.app.voice import VoiceConversation
    from jshi.core.envelope import SceneUtterance, SpeakerEvidence
    from jshi.voice.jev import VoiceJEV
    from tests.test_jev_split_modes import Model, entry
    async def run():
        messages=[]
        async def send(m):messages.append(m)
        invalid=entry(utterance_complete='incomplete',process_refs=[],pending_refs=['I1'])
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(Model([invalid]),mode='split',fallback_models=[]))
        try:
            speaker=SpeakerEvidence('typed',label='lux',object_id='lux-id',method='user_selected',status='recognized')
            row=SceneUtterance('匠石，现在开始',speaker,0,0,input_id='actual-start')
            decision, record, _=await c._judge((row,),speaker,False)
            assert decision.action=='respond' and record['fallback_to_main']
            assert record['route']=='main' and record['model_error']
            assert any('交主认知复核' in m.get('text','') for m in messages)
        finally:await c.close()
    asyncio.run(run())
