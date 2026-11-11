from jshi.core import SubjectState
from jshi.models import ModelRequest
from jshi.models.prompt import build_system, build_user


def request(**kw):
    return ModelRequest(purpose='subject_activity', subject_state=SubjectState('stone', '匠石', 'test'),
                        input_text='还有别的吗', persona_user_text='【片场】任务成功，匠石曾承诺稍后告知', **kw)


def test_absent_tool_payload_is_explicit_even_when_scene_claims_success():
    r = request()
    assert '本次没有载入【工具相关】正文' in build_user(r)
    assert '具体数值、日期及结论须有本次提供的对应来源' in build_system(r)
    assert '自己的旧回答' in build_system(r)


def test_tool_material_does_not_imply_result_is_complete():
    r = request(tool_input='【工具相关】任务仍在执行，尚无结果')
    assert '逐项区分状态记录与实际结果正文' in build_user(r)
    assert '本次没有载入' not in build_user(r)


def test_non_main_requests_do_not_get_main_material_scope():
    r = ModelRequest(purpose='voice_jev_entry', subject_state=SubjectState('stone', '匠石', 'test'), input_text='你好')
    assert '【本轮结果材料范围】' not in build_user(r)
    assert '【结果依据与当前交往】' not in build_system(r)
