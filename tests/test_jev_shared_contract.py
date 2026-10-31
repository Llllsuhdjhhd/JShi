from io import BytesIO
import json
from jshi.voice.jev_prompts import ENTRY_INSTRUCTION,ENTRY_OUTPUT_RULES,PREPARATION_OUTPUT_RULES
from jshi.voice.jev_serial import combined_instruction
from jshi.models.base import OpenAICompatibleModel,ModelRequest
from jshi.core import SubjectState


def test_combined_keeps_same_cross_field_rules_as_entry():
    combined=combined_instruction()
    assert ENTRY_OUTPUT_RULES in combined and ENTRY_OUTPUT_RULES in ENTRY_INSTRUCTION
    assert PREPARATION_OUTPUT_RULES in combined
    assert 'JSON整数' in PREPARATION_OUTPUT_RULES
    for rule in ('background的process_refs为空','prepare须有process_refs','wait须有pending_refs'):
        assert rule in combined


def test_base_adapter_preserves_transport_diagnostics(monkeypatch):
    import jshi.models.base as module
    monkeypatch.setattr(module,'_urlopen_chat',lambda *a,**kw:BytesIO(
        b'{"choices":[{"message":{"content":"{}"},"finish_reason":"stop"}],"usage":{"completion_tokens":2}}'))
    model=OpenAICompatibleModel('https://example.test/v1','fake','deepseek-v4-flash')
    response=model.generate(ModelRequest(purpose='voice_jev_entry',input_text='JSON',
        subject_state=SubjectState('stone','匠石','voice_jev_entry')))
    assert response.metadata['finish_reason']=='stop'
    assert response.metadata['response_format']=='json_object'
    assert response.metadata['completion_tokens']==2
