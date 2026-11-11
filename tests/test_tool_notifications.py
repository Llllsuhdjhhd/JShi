import hashlib
import json
from pathlib import Path

import pytest

from jshi.tool.verification import assess_pi, check_file
from jshi.subject import HistoryKind
from tests.test_voice import process


def test_completion_claim_has_no_evidence(tmp_path):
    assert assess_pi([], base=tmp_path)['verdict'] == 'unknown'
    assert not check_file('meeting.txt', base=tmp_path)['passed']


def test_real_file_write_and_mismatch(tmp_path):
    path = tmp_path / 'meeting.txt'
    path.write_text('会议记录\n第一项讨论', encoding='utf-8')
    calls = [{'name': 'write', 'args': {'path': str(path), 'content': path.read_text(encoding='utf-8')},
              'output': 'written', 'error': False}]
    checked = assess_pi(calls, base=tmp_path)
    assert checked['verdict'] == 'passed'
    assert checked['facts'][0]['path'] == str(path)
    path.write_text('另一段内容', encoding='utf-8')
    assert assess_pi(calls, base=tmp_path)['verdict'] == 'failed'


def test_shell_receipt_is_checked_against_actual_file(tmp_path):
    path = tmp_path / 'meeting.txt'
    path.write_bytes(b'actual meeting')
    payload = {'artifacts': [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}
    calls = [{'name': 'powershell', 'output': json.dumps(payload), 'error': False}]
    assert assess_pi(calls, base=tmp_path)['verdict'] == 'passed'
    path.unlink()
    assert assess_pi(calls, base=tmp_path)['verdict'] == 'failed'


def failed_task(p, owner='lux-id'):
    service = p.tool_service
    item = service.intake_store.create(subject_id='stone', object_id=owner,
                                      need='在桌面创建会议.txt并记录会议')
    service._fail_intake(item.intake_id, '策划模型响应超时，尚未开始执行')
    return item.intake_id


def test_notice_persisted_claimed_once_and_no_owner_leak(process):
    service = process.tool_service
    task = failed_task(process)
    service._publish_notice(task)
    assert service.claim_notices('stone', 'other') == ()
    assert service.claim_notices('stone', 'lux-id') == (task,)
    assert service.claim_notices('stone', 'lux-id') == ()
    service.release_notices((task,))
    from jshi.tool.work import WorkIndex
    assert WorkIndex(service.work_index.path).pending_notices('stone', 'lux-id')[0]['task_id'] == task
    service.release_notices((task,), handled=True)
    assert service.claim_notices('stone', 'lux-id') == ()
    service._publish_notice(task)
    assert service.claim_notices('stone', 'lux-id') == ()


def test_internal_result_uses_main_without_fabricating_user_input(process):
    task = failed_task(process)
    before = process.repository.list_history('stone', HistoryKind.FACT, 1000)
    result = process.experience_tool_results('stone', 'lux-id', (task,))
    assert result.current_state.prompt_level == 1
    assert result.current_state.recall_memory is False
    assert result.current_state.input_items[0]['source'] == 'tool'
    assert result.current_state.input_items[0]['actor_id'] == ''
    assert task in result.selected_tool_ids
    assert '尚未开始执行' in result.current_state.tool_input
    after = process.repository.list_history('stone', HistoryKind.FACT, 1000)
    fresh = [r for r in after if r.id not in {x.id for x in before}]
    assert any(r.event_type == 'tool_event' for r in fresh)
    assert not any(r.event_type == 'external_input' for r in fresh)
    assert process.tool_service.claim_notices('stone', 'lux-id') == ()


def test_internal_result_rejects_wrong_recipient(process):
    task = failed_task(process, owner='other')
    with pytest.raises(ValueError, match='owner mismatch'):
        process.experience_tool_results('stone', 'lux-id', (task,))


@pytest.mark.parametrize('active', [False, True])
def test_text_page_receives_result_without_user_asking_again(process, active):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    from jshi.voice.jev import VoiceJEV
    from jshi.models.prompt import build_user

    async def run():
        async with TestClient(TestServer(create_app(process, 'stone', VoiceConfig(''), jev=VoiceJEV(None)))) as client:
            headers = {'Origin': str(client.make_url('/')).rstrip('/')}
            if active:
                response = await client.post('/text-input', json={'object_id': 'lux-id', 'text': '你好'}, headers=headers)
                assert response.status == 200
                await response.read()
            task = failed_task(process)
            first = await client.post('/tool-results', json={'object_id': 'lux-id'}, headers=headers)
            assert first.status == 200
            payload = await first.json()
            assert payload['reply'] and payload['activity_id']
            request = process.cognition.requests[-1]
            assert request.cognition_mode == ('conversation' if active else 'initiative')
            assert '【说话人】' not in build_user(request)
            assert '来源：工具内部事件；不是人物发言' in build_user(request)
            again = await client.post('/tool-results', json={'object_id': 'lux-id'}, headers=headers)
            assert not (await again.json())['reply']
            assert process.tool_service.claim_notices('stone', 'lux-id') == ()
    asyncio.run(run())


def test_text_interrupts_tool_notice_before_starting_new_cognition(process):
    import asyncio
    from threading import Event
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    from jshi.voice.jev import VoiceJEV
    from jshi.subject.cancellation import check

    entered, exited, release = Event(), Event(), Event()
    original = process.cognition
    class Blocking:
        name = 'local-interruption-check'
        def generate(self, request):
            if request.cognition_mode == 'initiative':
                entered.set()
                try:
                    while not release.wait(.025):
                        check()
                finally:
                    exited.set()
            assert exited.is_set()
            return original.generate(request)
    process.cognition = Blocking()

    async def run():
        task = failed_task(process)
        async with TestClient(TestServer(create_app(process, 'stone', VoiceConfig(''), jev=VoiceJEV(None)))) as client:
            headers = {'Origin': str(client.make_url('/')).rstrip('/')}
            notice = asyncio.create_task(client.post('/initiative-results', json={'object_id': 'lux-id'}, headers=headers))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                response = await asyncio.wait_for(client.post('/text-input',
                    json={'object_id': 'lux-id', 'text': '先说说这个工具事项的情况'}, headers=headers), 5)
                assert response.status == 200
                await response.read()
                assert exited.is_set()
                assert not (await (await notice).json())['reply']
                assert original.requests[-1].cognition_mode == 'conversation'
                assert '尚未开始执行' in original.requests[-1].tool_input
            finally:
                release.set()
                await asyncio.gather(notice, return_exceptions=True)
    asyncio.run(run())


def test_unknown_completion_does_not_unlock_dependent_step(tmp_path):
    from jshi.tool import HangStore, ToolModule, ToolRunner, ToolService, ToolRequest, ToolFeedback, ToolResult, FeedbackKind
    from jshi.tool.plan import ToolPlan, ToolStep

    class Engine:
        name = 'unverified-test'
        def __init__(self): self.calls = []
        def execute(self, request):
            self.calls.append(request.command)
            return (ToolFeedback(request_id=request.request_id, kind=FeedbackKind.RESULT,
                                 result=ToolResult(summary='文件已创建', ideal=True)),)
    engine = Engine()
    store = HangStore(tmp_path / 'hang.jsonl')
    service = ToolService(store, ToolRunner(ToolModule(engine), store))
    intake = service.intake_store.create(subject_id='stone', object_id='lux-id', need='创建文件再写入')
    service._launch_plan(intake, ToolPlan('plan', '创建文件再写入', steps=(
        ToolStep('create', ToolRequest(command='create')),
        ToolStep('write', ToolRequest(command='write'), depends_on=('create',)))))
    service.drain_for_tests()
    assert engine.calls == ['create']
    assert service.claim_notices('stone', 'lux-id')


def test_jev_can_see_planning_failure_without_claiming_execution(process):
    from jshi.voice.jev_material import task_context
    from jshi.core.envelope import SpeakerEvidence
    failed_task(process)
    rows = task_context(process, 'stone', [SpeakerEvidence('text', 'lux-id', 'lux', 'recognized', 'user_selected')])
    assert rows[0]['state'] == '策划未完成，尚未执行'
    assert rows[0]['result_available'] is False
    assert rows[0]['completion_verified'] is False

def test_failed_notice_is_not_an_automatic_retry_loop(process):
    from jshi.tool.work import WorkIndex
    task = failed_task(process)
    class Broken:
        name = 'failed-local-model'
        def generate(self, request): raise RuntimeError('offline simulated failure')
    process.cognition = Broken()
    with pytest.raises(RuntimeError):
        process.experience_tool_results('stone', 'lux-id', (task,))
    process.tool_service.release_notices((task,))
    assert process.tool_service.claim_notices('stone', 'lux-id') == ()
    persisted = WorkIndex(process.tool_service.work_index.path)
    assert persisted.pending_notices('stone', 'lux-id')[0]['attempted'] is True
    assert not persisted.for_task(task)[0]['closed']


@pytest.mark.parametrize('operation', ['cancel', 'invalidate'])
def test_obsolete_notice_cannot_be_delivered(process, operation):
    task = failed_task(process)
    getattr(process.tool_service, operation)(task)
    assert process.tool_service.claim_notices('stone', 'lux-id') == ()
    with pytest.raises(ValueError, match='no longer available'):
        process.experience_tool_results('stone', 'lux-id', (task,))


def test_query_evidence_can_feed_next_step_without_claiming_artifact_verification(tmp_path):
    from jshi.tool import ToolResult
    from jshi.tool.verification import can_advance
    evidence = assess_pi([{'name': 'powershell', 'output': '{"results":["observed fact"]}',
                           'substantive': True, 'error': False}], base=tmp_path)
    assert evidence['verdict'] == 'unknown'
    assert can_advance(ToolResult(execution=(evidence,)))
    evidence['facts'] = [{'passed': False}]
    evidence['verdict'] = 'failed'
    assert not can_advance(ToolResult(execution=(evidence,)))


def test_planning_uses_background_timeout():
    from jshi.app.model_settings import ModelSettings
    profile = {'choices': [{'service': 'test', 'model': 'deepseek-chat'}]}
    # Same route configuration as the application, without opening a connection.
    services = {'test': {'protocol': 'openai', 'endpoint': 'https://api.deepseek.com', 'api_key': 'unused'}}
    route = ModelSettings._chain(None, profile, services, 'tool_plan')
    assert route[0].timeout == 45.0

def test_voice_notice_runs_main_and_waits_for_mock_playback_receipt(process):
    import asyncio
    from jshi.app.voice import VoiceConversation
    from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence
    from tests.test_voice import FakeCloud
    async def run():
        sent = []
        async def send(row): sent.append(row)
        conversation = VoiceConversation(process, 'stone', FakeCloud(), send, scene_idle_s=60)
        task = failed_task(process)
        ids = process.tool_service.claim_notices('stone', 'lux-id')
        message = InputEnvelope('test', 'tool_event', (InputPart('text', '工具结果回报'),),
            SpeakerEvidence('recipient', 'lux-id', 'lux', 'recognized', 'tool_recipient'),
            voice_delivery=True, tool_event_ids=ids)
        try:
            conversation.turn_queue.put_nowait((message, conversation.epoch))
            await asyncio.wait_for(conversation.turn_queue.join(), 8)
            for _ in range(100):
                if any(row['type'] == 'audio' for row in sent): break
                await asyncio.sleep(.01)
            assert any(row['type'] == 'audio' for row in sent)
            assert not conversation.delivery.snapshot()['played_text']
            rid = conversation.delivery.snapshot()['reply_id']
            await conversation.acknowledge({'reply_id': rid, 'segment': 0, 'phase': 'completed'})
            assert conversation.delivery.snapshot()['played_text']
            assert len(process.cognition.requests) == 1
            assert process.cognition.requests[0].purpose == 'subject_activity'
            facts = process.repository.list_history('stone', HistoryKind.FACT, 1000)
            assert not any(r.event_type == 'external_input' for r in facts)
            assert process.tool_service.claim_notices('stone', 'lux-id') == ()
        finally:
            await conversation.close()
    asyncio.run(run())

def test_user_followup_consumes_notice_without_second_reply(process):
    from jshi.models import ModelResponse
    from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence
    task = failed_task(process)
    class Answer:
        name = 'local-followup'
        def generate(self, request):
            return ModelResponse(model=self.name, text='策划超时，尚未开始执行。',
                tool_handling=({'task_id':'T1','disposition':'answered','evidence':'策划超时，尚未开始执行。','work_complete':False},))
    process.cognition = Answer()
    e = InputEnvelope('test','web_text',(InputPart('text','会议文件怎么样了？'),),
        SpeakerEvidence('text','lux-id','lux','recognized','user_selected'))
    result = process.experience('stone',e.text,envelope=e)
    assert task in result.selected_tool_ids
    assert process.tool_service.claim_notices('stone','lux-id') == ()


def test_reused_result_still_notifies_without_reexecuting(tmp_path):
    from tests.test_tool_work_reuse import runtime, launch
    from jshi.tool import ToolRequest
    from jshi.tool.reuse import ReusePolicy
    service, engine = runtime(tmp_path, {'weather':ReusePolicy(600,('city',))})
    first = launch(service,ToolRequest(command='weather',params={'city':'x'}))
    second = launch(service,ToolRequest(command='weather',params={'city':'x'}))
    assert len(engine.calls) == 1
    assert set(service.claim_notices('s','o')) == {first,second}

def test_work_reference_compacts_and_restores_without_cross_person_leak(process, monkeypatch):
    from jshi.models import ModelResponse, ToolUseIntent
    from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence
    task = failed_task(process)
    seen = []
    class Continue:
        name = 'local-continuation'
        def generate(self, request):
            seen.append(request)
            return ModelResponse(model=self.name, text='我再试一次。',
                tool_intent=ToolUseIntent(need='继续创建',work_id='W1'))
    process.cognition = Continue()
    intake = []
    monkeypatch.setattr(process.tool_service,'intake',lambda **kw:intake.append(kw))
    e=InputEnvelope('test','web_text',(InputPart('text','继续创建会议文件'),),
        SpeakerEvidence('text','lux-id','lux','recognized','user_selected'))
    result=process.experience('stone',e.text,envelope=e)
    assert '事项ID：W1' in result.current_state.tool_input
    assert task not in result.current_state.tool_input
    assert intake[0]['work_id'] == process.tool_service.work_index.for_task(task)[0]['work_id']
    assert dict(result.current_state.work_codes)['W1'] == intake[0]['work_id']
