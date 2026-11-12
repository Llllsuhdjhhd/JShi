from dataclasses import replace

from jshi.skill.tool_plan import ToolPlanSkill
from jshi.tool.discovery import ToolIndex
from jshi.tool.intake import IntakeRecord
from jshi.tool.pi_engine import PiEngine
from jshi.tool.service import _plan_to_dict


def test_file_verification_keeps_its_own_goal_in_actual_skill_request():
    skill = ToolPlanSkill(object())
    plan = skill.parse({'ok': True, 'plan': {'mode': 'sequential', 'steps': [
        {'step_id': 'write', 'command': 'skill:write', 'need': '追加本轮原话',
         'params': {'mode': 'append', 'content': '你喜欢什么小说'}, 'expected_result': '原话已追加且旧内容保留'},
        {'step_id': 'verify', 'command': 'skill:read', 'params': {'open_mode': 'none'},
         'expected_result': '只读核验目标文件存在且内容匹配', 'depends_on': ['write']},
    ]}})
    intake = IntakeRecord(intake_id='i', subject_id='s', object_id='o',
                          need='创建文件、写入内容，并核验')
    tools = [{'name': 'skill:write'}, {'name': 'skill:read'}]
    final = skill._finalize_plan(intake, plan, {t['name'] for t in tools}, ToolIndex(tools))
    assert final.need == intake.need
    assert final.steps[0].request.need == '追加本轮原话'
    verify = final.steps[1].request
    assert verify.need == '只读核验目标文件存在且内容匹配'
    prompt = PiEngine()._build_prompt(verify)
    assert prompt.startswith('/skill:read 只读核验目标文件存在且内容匹配')
    assert '本步期望结果：只读核验目标文件存在且内容匹配' in prompt
    assert '总目标背景（不是本步执行指令）：' + intake.need in prompt
    assert '不得创建、覆盖或修补' in prompt
    assert _plan_to_dict(final)['steps'][1]['need'] == verify.need
    loop_request = replace(verify, need=intake.need, meta={'agent_plan': _plan_to_dict(final)})
    loop_prompt = PiEngine()._build_prompt(loop_request)
    assert '本步目标：追加本轮原话' in loop_prompt
    assert '本步验收：原话已追加且旧内容保留' in loop_prompt
    assert '本步验收：只读核验目标文件存在且内容匹配' in loop_prompt
    payload = skill._task_payload(intake, (), ToolIndex(tools), '', ())
    assert payload['execution_capabilities']['future_dialogue_input'] is False


def test_step_without_description_does_not_inherit_entire_work():
    skill = ToolPlanSkill(object())
    plan = skill.parse({'ok': True, 'plan': {'steps': [{'step_id': 'read', 'command': 'skill:read'}]}})
    intake = IntakeRecord(intake_id='i', subject_id='s', object_id='o', need='创建并核验文件')
    final = skill._finalize_plan(intake, plan, {'skill:read'}, ToolIndex([{'name': 'skill:read'}]))
    assert final.steps[0].request.need != intake.need
    assert final.steps[0].request.meta['overall_need'] == intake.need
