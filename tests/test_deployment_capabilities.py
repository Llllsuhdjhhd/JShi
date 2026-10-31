from jshi.models import ModelRequest
from jshi.core import SubjectState
from jshi.models.prompt import build_system,DEPLOYMENT_CAPABILITIES
import pytest

@pytest.mark.parametrize('purpose',['subject_activity','voice_jev','voice_jev_entry','voice_jev_prepare','voice_jev_identity','voice_jev_scene'])
def test_live_request_includes_current_deployment_boundary_once(purpose):
    request=ModelRequest(purpose=purpose,input_text='匠石，我们下去玩了啊。',subject_state=SubjectState('test','匠石','对话'))
    text=build_system(request)
    assert text.count(DEPLOYMENT_CAPABILITIES)==1
    assert text.index('【当前部署的身份与能力】')<text.index('【文字规范】')
    assert '自身不具备物理意义上的移动、操控等能力' in text

def test_unrelated_request_has_no_injected_robot_identity():
    request=ModelRequest(purpose='transcription',input_text='文本',subject_state=SubjectState('test','测试','任务'))
    assert DEPLOYMENT_CAPABILITIES not in build_system(request)
