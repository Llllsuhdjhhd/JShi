from dataclasses import replace

import pytest

from jshi.core.envelope import InputEnvelope, InputPart, SceneUtterance, SpeakerEvidence
from jshi.skill.tool_plan import ToolPlanSkill
from jshi.tool import HangStore, ToolModule, ToolRequest, ToolRunner, ToolService
from jshi.tool.discovery import ToolIndex
from jshi.tool.intake import IntakeRecord
from jshi.tool.recording import COMMAND, TOOL, DialogueRecorder


def recorder(tmp_path, **kw):
    return DialogueRecorder(tmp_path / 'journal.db', desktop=tmp_path, **kw)


def test_preserves_old_content_deduplicates_and_updates_asr_revision(tmp_path):
    target = tmp_path / 'notes.txt'
    target.write_text('已有内容', encoding='utf-8')
    store = recorder(tmp_path)
    store.observe('s', 'o', '1', '从现在开始记')
    store.command('s', 'o', 'w', {'action': 'start', 'path': str(target)}, start_seq=1)
    store.observe('s', 'o', '2', '你喜欢什么小')
    store.observe('s', 'o', '2', '你喜欢什么小说')
    store.observe('s', 'o', '2', '你喜欢什么小说')
    store.observe('s', 'other', '3', '另一个人的话')
    content = target.read_text(encoding='utf-8')
    assert content.startswith('已有内容\n')
    assert content.count('你喜欢什么小说') == 1
    assert '另一个人的话' not in content
    assert store.for_work('w')[0]['state'] == 'running'


def test_stop_scoped_to_owner_and_not_quoted_or_negated(tmp_path):
    store = recorder(tmp_path)
    row = store.command('s', 'o', 'w', {'action': 'start', 'scope': 'all'})
    store.observe('s', 'other', '1', '停止记录')
    store.observe('s', 'o', '2', '不要停止记录')
    store.observe('s', 'o', '3', '他说“停止记录”')
    assert store.for_work('w')[0]['state'] == 'running'
    store.observe('s', 'o', '4', '匠石，请停止记录。')
    store.observe('s', 'o', '5', '不应写入')
    assert store.for_work('w')[0]['state'] == 'stopped'
    from pathlib import Path
    text = Path(row['path']).read_text(encoding='utf-8')
    assert '不要停止记录' in text and '他说“停止记录”' in text
    assert '不应写入' not in text and '匠石，请停止记录。' not in text


def test_external_edit_blocks_without_overwrite_then_resume_catches_up(tmp_path):
    changes = []
    store = recorder(tmp_path, on_change=changes.append)
    target = tmp_path / 'notes.txt'
    store.command('s', 'o', 'w', {'action': 'start', 'path': str(target)})
    store.observe('s', 'o', '1', '第一句')
    original = target.read_bytes()
    target.write_text('用户自行修改', encoding='utf-8')
    store.observe('s', 'o', '2', '不能丢失的第二句')
    store.observe('s', 'o', '3', '不能丢失的第三句')
    assert target.read_text(encoding='utf-8') == '用户自行修改'
    assert [r['state'] for r in changes] == ['blocked']
    assert store.command('s', 'o', 'w', {'action': 'status'})['state'] == 'blocked'
    target.write_bytes(original)  # User resolves the conflict; resume never discards external changes.
    restarted = recorder(tmp_path)
    assert restarted.command('s', 'o', 'w', {'action': 'resume'})['state'] == 'running'
    assert '不能丢失的第二句' in target.read_text(encoding='utf-8')
    assert '不能丢失的第三句' in target.read_text(encoding='utf-8')


def test_restarts_and_repeated_start_preserve_cursor_without_duplicate_lines(tmp_path):
    target = tmp_path / 'notes.txt'
    first = recorder(tmp_path)
    row = first.command('s', 'o', 'w', {'action': 'start', 'path': str(target)})
    first.observe('s', 'o', '1', '第一句')
    second = recorder(tmp_path)
    assert second.command('s', 'o', 'w2', {'action': 'start', 'path': str(target)})['id'] == row['id']
    assert second.for_work('w2')[0]['state'] == 'running'
    second.observe('s', 'o', '2', '第二句')
    second.command('s', 'o', 'w', {'action': 'stop'})
    assert target.read_text(encoding='utf-8').count('第一句') == 1
    assert target.read_text(encoding='utf-8').count('第二句') == 1


def test_recording_failure_is_recoverable_and_other_owner_cannot_control(tmp_path):
    store = recorder(tmp_path)
    with pytest.raises(ValueError):
        store.command('s', 'o', 'w', {'action': 'start', 'path': 'relative.txt'})
    store.command('s', 'o', 'w', {'action': 'start'})
    with pytest.raises(ValueError):
        store.command('s', 'other', 'w', {'action': 'stop'})
    assert store.for_work('w')[0]['state'] == 'running'


class NeverEngine:
    name = 'never'

    def execute(self, request):
        raise AssertionError('Persistent recording must not use a model/Pi for each input')


def service_at(path):
    store = HangStore(path / 'hang.jsonl')
    return ToolService(store, ToolRunner(ToolModule(NeverEngine()), store))


def test_failed_continuous_goal_survives_restart_and_one_off_backfill(tmp_path):
    service = service_at(tmp_path)
    target = tmp_path / 'later' / 'record.txt'
    service.observe_envelope('s', envelope('后面也一直记', '1'))
    intake = service.intake_store.create(subject_id='s', object_id='o', need='持续记录直到停止')
    service._launch(intake, ToolRequest(command=COMMAND,
        params={'action': 'start', 'path': str(target), 'history': 'available'}))
    service.drain_for_tests()
    goal = service.work_index.get(intake.intake_id)
    assert goal['recording_goal']['continuous']
    assert not service._work_execution_done(goal)
    target.parent.mkdir()
    restarted = service_at(tmp_path)
    fix = restarted.intake_store.create(subject_id='s', object_id='o', need='补进去')
    fix = restarted.intake_store.update(fix.intake_id, meta={'work_id': intake.intake_id})
    restarted._launch(fix, ToolRequest(command=COMMAND, params={'action': 'export', 'path': str(target)}))
    restarted.drain_for_tests()
    assert restarted.recorder.for_work(intake.intake_id)[0]['state'] == 'running'
    restarted.observe_envelope('s', envelope('补录之后的新一句', '2'))
    assert '补录之后的新一句' in target.read_text('utf-8')
    assert not restarted._work_execution_done(restarted.work_index.get(intake.intake_id))
    restarted.observe_envelope('s', envelope('停止记录', '3'))
    assert not restarted.work_index.get(intake.intake_id)['recording_goal']['continuous']


def test_recording_supervision_detects_external_change_without_new_speech(tmp_path):
    service = service_at(tmp_path)
    target = tmp_path / 'record.txt'
    intake = service.intake_store.create(subject_id='s', object_id='o', need='持续记录')
    service._launch(intake, ToolRequest(command=COMMAND, params={'action': 'start', 'path': str(target)}))
    service.drain_for_tests()
    initial = service.claim_notices('s', 'o')
    service.release_notices(initial, handled=True)
    target.write_text('外部修改必须保留', encoding='utf-8')
    changes = service.check_recordings('s', force=True)
    assert len(changes) == 1 and changes[0]['state'] == 'blocked'
    assert target.read_text('utf-8') == '外部修改必须保留'
    assert service.check_recordings('s', force=True) == ()
    notices = service.claim_notices('s', 'o')
    assert len(notices) == 1
    service.release_notices(notices, handled=True)


def envelope(text, key='1', owner='o'):
    return InputEnvelope('typed', 'web_text', (InputPart('text', text),),
                         SpeakerEvidence('t', owner, '小林', 'recognized', 'user_selected', 1), input_id=key)


def test_host_tool_receives_text_and_direct_voice_without_main_and_keeps_work_open(tmp_path):
    service = service_at(tmp_path)
    target = tmp_path / 'record.txt'
    command = envelope('之后听到的也记进去')
    service.observe_envelope('s', command)
    intake = service.intake_store.create(subject_id='s', object_id='o', need='持续记录直到我说停')
    intake = service.intake_store.update(intake.intake_id, meta={'recording_start_seq': '1'})
    # Input arrives while planning is still running.
    service.observe_envelope('s', envelope('规划期间的一句', '2'))
    task = service._launch(intake, ToolRequest(command=COMMAND, params={'action': 'start', 'path': str(target)}))
    service.drain_for_tests()
    speech = SceneUtterance('你喜欢什么小说', command.speaker, 0, 1000, input_id='3')
    service.observe_utterances('s', [speech])  # Same hook as JEV direct/background route.
    service.observe_envelope('s', replace(command, current_utterances=(speech,)))  # Main dedup.
    service.observe_envelope('s', replace(envelope('内部事件不记录', '4'), source='tool_event'))
    service.observe_envelope('s', replace(envelope('未结束的假设', '5'), final=False))
    text = target.read_text(encoding='utf-8')
    assert '规划期间的一句' in text
    assert text.count('你喜欢什么小说') == 1
    assert '内部事件不记录' not in text and '未结束的假设' not in text
    work = service.work_index.for_task(task)[0]
    assert not service._work_execution_done(work)
    entries = service.list_tool_related_entries('s', 'o')
    assert any(e['status'] == '持续记录中' for e in entries)
    service.observe_envelope('s', envelope('停止记录', '6'))
    assert service._work_execution_done(service.work_index.get(work['work_id']))
    assert service.claim_notices('s', 'o')


def test_cancelling_started_recording_stops_future_writes(tmp_path):
    service = service_at(tmp_path)
    target = tmp_path / 'record.txt'
    intake = service.intake_store.create(subject_id='s', object_id='o', need='持续记录')
    task = service._launch(intake, ToolRequest(command=COMMAND, params={'action': 'start', 'path': str(target)}))
    service.drain_for_tests()
    assert service.cancel(task)
    before = target.read_bytes()
    service.observe_envelope('s', envelope('取消后的一句'))
    assert target.read_bytes() == before


def test_planner_knows_runtime_capability_and_does_not_delegate_it_to_pi():
    skill = ToolPlanSkill(object())
    intake = IntakeRecord(intake_id='i', subject_id='s', object_id='o', need='持续记录')
    index = ToolIndex.from_maps([TOOL])
    payload = skill._task_payload(intake, (), index, '', ())
    assert payload['execution_capabilities']['future_dialogue_input']
    assert 'export' in payload['engine_tools'][0]['params']['properties']['action']['enum']
    plan = skill.parse({'ok': True, 'plan': {'mode': 'agent_loop', 'steps': [
        {'step_id': 'start', 'command': COMMAND, 'params': {'action': 'start'}}]}})
    from jshi.tool.plan import PlanFailure
    assert isinstance(skill._finalize_plan(intake, plan, index.names, index), PlanFailure)


def test_all_scope_keeps_unidentified_final_speech_without_assigning_it_to_owner(tmp_path):
    service = service_at(tmp_path)
    target = tmp_path / 'all.txt'
    service.recorder.command('s', 'o', 'w', {'action': 'start', 'scope': 'all', 'path': str(target)})
    unknown = SceneUtterance('不确定是谁说的', SpeakerEvidence('unknown'), 0, 100, input_id='unknown')
    service.observe_utterances('s', [unknown])
    text = target.read_text(encoding='utf-8')
    assert '身份未确认的说话人：不确定是谁说的' in text
    identified = replace(unknown, speaker=SpeakerEvidence('t', 'o', '小林', 'recognized'))
    service.observe_utterances('s', [identified])
    text = target.read_text(encoding='utf-8')
    assert text.count('不确定是谁说的') == 1
    assert '小林：不确定是谁说的' in text
    assert '身份未确认的说话人' not in text


def test_start_before_retained_window_discloses_missing_input(tmp_path):
    store = recorder(tmp_path)
    for i in range(205):
        store.observe('s', 'o', str(i), '输入' + str(i))
    row = store.command('s', 'o', 'w', {'action': 'start'}, start_seq=1)
    assert row['input_gap']
    assert '未确认全量记录' in store.summary(row)


def test_host_recording_can_be_planned_when_pi_is_unavailable():
    from jshi.skill.tool_plan import SkillPlanner
    class Skill:
        def plan_intake(self, intake, catalog, tools, **kw):
            assert [t['name'] for t in tools] == [COMMAND]
            return ToolRequest(command=COMMAND, params={'action': 'start'})
    from types import SimpleNamespace
    planner = SkillPlanner(Skill(), engine_tools=[], engine=SimpleNamespace(_spawn_error='offline'))
    planner.extra_tools = (TOOL,)
    result = planner.plan(IntakeRecord(intake_id='i', subject_id='s', object_id='o', need='记录'))
    assert result.command == COMMAND


def test_real_intake_plan_start_then_dream_and_both_reply_routes(tmp_path):
    """The missed production chain, including planning, real sources and restart."""
    import json
    from jshi.skill.tool_plan import SkillPlanner
    from jshi.models import ModelResponse, ResponsePlan, ResponseItem
    from jshi.voice.dialogue import VoiceDialogue
    target = tmp_path / 'conversation.txt'
    target.write_text('已有记录\n', encoding='utf-8')
    calls = []

    class Model:
        name = 'offline-planner'
        def generate(self, request):
            calls.append(request)
            payload = json.dumps({'ok': True,
                'task_type': 'dialogue_recording', 'requirements': ['记录双方实际对话并持续接收后续内容'],
                'dialogue_recording': {'action': 'start', 'history': 'available', 'path': str(target)}})
            return ModelResponse(model=self.name, response_plan=ResponsePlan('respond', items=(ResponseItem('verbal', payload),)))

    service = service_at(tmp_path)
    service.planner = SkillPlanner(ToolPlanSkill(Model()), engine_tools=[{'name':'skill:append-file'}])
    service.planner.extra_tools = (TOOL,)
    dialogue = VoiceDialogue(tmp_path / 'dialogue.json')
    dialogue.on_reply = service.observe_reply
    service.recording_source = dialogue.recording_snapshot
    service.observe_envelope('s', envelope('后续的也都记录进去啊', 'start'))
    service.observe_envelope('s', envelope('就是我们的对话记录进去就可以了', 'clarify'))
    intake = service.intake(subject_id='s', object_id='o', activity_id='a',
        need='把这一轮对话追加到文件', user_input='就是我们的对话记录进去就可以了')
    service.drain_for_tests()
    assert len(calls) == 1
    assert '后续的也都记录进去啊' in calls[0].input_text
    assert service.intake_store.get(intake.intake_id).status == 'launched'
    for key, text in [('plane', '我梦到了一架很大的飞机，降落在老家的桑树地'),
                      ('people', '好像下来3个美国人'), ('fight', '是的。我好像和他们打了起来')]:
        service.observe_envelope('s', envelope(text, key))
    dialogue.plan('s', 'text:fight', 'web-text', ['后来你跑开了吗？'], shown=True, owner='o')
    dialogue.plan('s', 'text:meaning', 'web-text', ['梦里的情绪挺明显的。'], shown=True, owner='o')
    dialogue.plan('s', 'voice-unplayed', 'voice', ['不能算说过的句子'], owner='o')
    dialogue.plan('s', 'voice-played', 'voice', ['停止记录', '尚未播出的后半句'], owner='o')
    dialogue.bind('voice-played', 'delivery')
    dialogue.played('delivery', 0)  # Assistant wording cannot stop the job.
    service.observe_envelope('s', envelope('我还是挺喜欢这种梦的', 'like'))
    service.observe_envelope('s', envelope('另一个人的私聊', 'other', owner='other'))
    dialogue.plan('s', 'text:other', 'web-text', ['另一个人的回复'], shown=True, owner='other')
    row = service.recorder.list_for('s', 'o')[0]
    assert row['state'] == 'running'
    before = target.read_bytes()
    service.recorder.command('s', 'o', 'status', {'action': 'status'})
    assert target.read_bytes() == before
    restarted = service_at(tmp_path)
    restarted.observe_envelope('s', envelope('重启后继续的一句', 'after-restart'))
    restarted.observe_envelope('s', envelope('停止记录', 'stop'))
    restarted.observe_envelope('s', envelope('停止后的话', 'after-stop'))
    text = target.read_text(encoding='utf-8')
    for expected in ['已有记录', '桑树地', '3个美国人', '打了起来', '后来你跑开了吗', '梦里的情绪', '我还是挺喜欢', '重启后继续']:
        assert expected in text
    for excluded in ['不能算说过', '尚未播出', '另一个人', '停止后的话', '任务说明', '写入结果']:
        assert excluded not in text
    assert text.count('后来你跑开了吗') == 1
    assert len(calls) == 1  # Neither future input nor reply needs a model call.


def test_historical_export_and_since_do_not_invent_content(tmp_path):
    store = recorder(tmp_path)
    store.observe('s', 'o', 'later', '较晚原话', stamp='2026-11-12T11:42:00+00:00')
    store.observe('s', 'o', 'earlier', '补回的较早原话', stamp='2026-11-12T11:39:00+00:00', control=False)
    row = store.command('s', 'o', 'w', {'action': 'export', 'since': '2026-11-12T19:40:00+08:00'})
    from pathlib import Path
    assert row['state'] == 'stopped'
    text = Path(row['path']).read_text(encoding='utf-8')
    assert '较晚原话' in text and '补回的较早原话' not in text
    with pytest.raises(ValueError, match='聊天正文'):
        store.command('s', 'o', 'bad', {'action': 'start', 'content': '模型伪造的对话'})


def test_request_range_excludes_late_replayed_older_replies(tmp_path):
    store = recorder(tmp_path)
    store.observe('s', 'o', 'request', '从现在开始', stamp='2026-11-12T11:40:00+00:00')
    store.observe('s', 'o', 'older-reply', '委托前的旧回复', '匠石', '2026-11-12T11:39:00+00:00', role='assistant')
    row = store.command('s', 'o', 'w', {'action':'start'}, start_seq=1)
    from pathlib import Path
    assert '委托前' not in Path(row['path']).read_text(encoding='utf-8')
    with pytest.raises(ValueError, match='没有可核验'):
        store.command('s', 'other', 'empty', {'action':'export'})
    store.command('s', 'o', 'w2', {'action':'start', 'history':'available'})
    assert '委托前的旧回复' in Path(row['path']).read_text(encoding='utf-8')
    assert Path(row['path']).read_text(encoding='utf-8').count('从现在开始') == 1


def test_runtime_recorder_visible_in_large_catalog_and_malformed_plan_repaired():
    import json
    from jshi.models import ModelResponse, ResponsePlan, ResponseItem
    class Model:
        name = 'offline'
        calls = 0
        def generate(self, request):
            self.calls += 1
            result = ({'ok':True, 'task_type':'general', 'plan': {'steps':[{'step_id':'missing'}]}}
                      if self.calls == 1 else {'ok':True, 'task_type':'dialogue_recording',
                          'dialogue_recording': {'action':'status'}})
            return ModelResponse(model=self.name, response_plan=ResponsePlan('respond', items=(ResponseItem('verbal', json.dumps(result)),)))
    model = Model()
    skill = ToolPlanSkill(model)
    tools = [TOOL] + [{'name':f'skill:other-{i}', 'description':'unrelated'} for i in range(80)]
    intake = IntakeRecord(intake_id='i', subject_id='s', object_id='o', need='核对对话记录')
    payload = skill._task_payload(intake, (), ToolIndex.from_maps(tools), '', ())
    assert payload['runtime_tools'] == [TOOL]
    result = skill.plan_intake(intake, (), tools)
    assert result.command == COMMAND and model.calls == 2


def test_export_evidence_contains_actual_content_and_old_inspection_is_dated(tmp_path):
    from jshi.tool.service import format_tool_related
    from jshi.tool.contract import ToolFeedback, ToolResult, FeedbackKind, ToolStatus
    service = service_at(tmp_path)
    target = tmp_path / 'notes.txt'
    target.write_text('先前内容\n', encoding='utf-8')
    old = service.hang_store.create(subject_id='s', object_id='scene:s', command='read', need='核对 '+str(target))
    service.hang_store.append_feedback(old.task_id, (ToolFeedback(request_id=old.request_id,
        kind=FeedbackKind.RESULT, result=ToolResult(status=ToolStatus.OK, summary='梦的内容还没写入')),))
    service.hang_store.set_wrap(old.task_id, visible=True, summary='梦的内容还没写入', terminal=True)
    service.observe_envelope('s', envelope('飞机降落在桑树地', 'dream'))
    intake = service.intake_store.create(subject_id='s', object_id='o', need='补写我们的对话')
    task = service._launch(intake, ToolRequest(command=COMMAND,
        params={'action':'export', 'history':'available', 'path':str(target)}))
    service.drain_for_tests()
    fresh = service.list_tool_related_entries('s','o',required_ids=(task,))[0]
    assert '飞机降落在桑树地' in fresh['final_result']
    assert '后续没有持续记录' in fresh['final_result']
    older = service.list_tool_related_entries('s','scene:s',required_ids=(old.task_id,))
    brief = format_tool_related(older)
    assert '此前“未写入”等结论只对应旧文件' in brief
    assert '历史检查（不能代表当前状态）' in brief
    assert '飞机降落在桑树地' not in brief  # No cross-owner transcript disclosure.
    target.write_text('被外部改动', encoding='utf-8')
    changed = service.list_tool_related_entries('s','scene:s',required_ids=(old.task_id,))[0]['artifact_update']
    assert '不一致或无法读取' in changed and '仍在运行' not in changed
