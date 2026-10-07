from dataclasses import replace
import asyncio

import pytest

from jshi.core.prompt_profile import PromptProfile, normalize_hint, select_profile
from jshi.style.packs import (PERSONA_TOOL_NOTE,
                              reply_instruction_for, write_instruction_for)
from jshi.assembly.port import AssemblyFragment
from jshi.app.voice import VoiceConversation
from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV
from jshi.voice.volc import Transcript
from tests.test_voice import process, FakeCloud


def test_defaults_preserve_full_prompt_and_levels_remove_functions():
    from jshi.style.packs import reply_schema_for
    texts = [reply_instruction_for('smith', prompt_level=i) for i in range(1, 4)]
    assert texts[0] == reply_instruction_for('smith')
    assert 2000 <= len(texts[2]) <= 3000
    assert len(texts[0]) > len(texts[1]) > len(texts[2])
    assert PERSONA_TOOL_NOTE in texts[0]
    for level, text in enumerate(texts[1:], 2):
        assert '工具' not in text
        assert set(reply_schema_for('smith', prompt_level=level)['properties']) == {'mode','reply','action','unsaid','reason','reply_targets'}
    assert '事件发生时间' in texts[2]
    assert '回忆：' in texts[2]


def test_modules_do_not_restore_tools_into_short_schema():
    text = reply_instruction_for('smith', prompt_level=3, prompt_modules=('tool',))
    assert PERSONA_TOOL_NOTE not in text


@pytest.mark.parametrize('hint', [None, {}, {'level': True}, {'level': 0},
                                     {'level': 6}, {'level': 1, 'needs': ['made_up']},
                                     {'level': 1, 'needs': 'tool'}])
def test_invalid_hints_cannot_reduce_default(hint):
    assert normalize_hint(hint) == {}
    assert select_profile(['A'], [{'input_ids': ['A'], 'main_prompt_hint': hint}]).level == 1


def test_merge_requires_current_batch_coverage_and_preserves_modules():
    a = {'input_ids': ['A'], 'main_prompt_hint': {'level': 1, 'needs': []}}
    b = {'input_ids': ['B'], 'main_prompt_hint': {'level': 3, 'needs': ['tool']}}
    assert select_profile(['A', 'B'], [a]).level == 1
    assert select_profile(['C'], [a, b]).level == 1
    assert select_profile(['A', 'B'], [a, b]) == PromptProfile(1)
    assert select_profile(['B'], [b]).level == 3
    assert select_profile(['A'], [{**a, 'timed_out': True}]).level == 1
    # A JEV batch may include an unrelated utterance removed by the entry gate.
    assert select_profile(['A'], [{**a, 'input_ids': ['A', 'discarded']}]).level == 1


def test_short_main_keeps_memory_person_and_writer_memory(process):
    process.style_packs.set('stone', 'smith')
    process.zone_store.apply_edit('stone', (), append_blocks=('双方刚才约定下午三点继续。',))
    current = process.assemble_current_state('stone', '你好')
    current = replace(current, prompt_level=3, fragments=(
        AssemblyFragment('memory', 'm', '一年前去了黄山', 'event', object_id='lux-id'),
        AssemblyFragment('person_portrait', 'p', '喜欢安静', 'portrait', object_id='lux-id'),
    ))
    main = process._persona_user_text(current, boot=False)
    writer = process._persona_user_text(current, boot=False, include_tool=False, live_zone=True)
    assert '双方刚才约定下午三点继续' in main
    assert '【你此时的回忆】' in main
    assert '【人物肖像' in main
    assert '喜欢安静' in main
    assert '【片场字数】' not in main
    assert '【你此时的回忆】' in writer
    assert '【人物肖像' in writer
    with_tool = replace(current, tool_input='【工具相关】\n部分完成，附件未交付')
    assert not process._prompt_profile(with_tool).includes('tool')
    assert process._prompt_profile(with_tool).level == 3
    assert process._prompt_profile(with_tool).scene_view == 1
    instruction = process._persona_fields('stone', with_tool)[0]
    assert PERSONA_TOOL_NOTE not in instruction
    assert '【工具相关】' not in process._persona_user_text(with_tool, boot=False)


def test_one_writer_prompt_keeps_contract_and_classifies_blocks():
    text = write_instruction_for('smith')
    assert 'B1 是固定自我介绍' in text
    assert '不把partial_text整句当成已听完' in text
    assert '不要用 add 重复录入' in text
    assert '仍只整理一份片场' in text
    assert '"op":"tier"' in text
    assert [PromptProfile(i).scene_view for i in range(1, 4)] == [3, 2, 1]


def test_person_summary_views_use_complete_existing_versions(process):
    short = '喜欢先核对证据；这是过往观察，不是已确认的永久偏好。'
    long = short + '一次交往的详细背景。' * 30
    fragment = AssemblyFragment('person_portrait', 'p', long, 'portrait',
                                object_id='lux-id', alternatives=(long, short))
    assert short in process._person_portrait_lines((fragment,), budget_chars=200)[0]
    assert long not in process._person_portrait_lines((fragment,), budget_chars=200)[0]
    # Legacy overlong dossiers cannot bypass the hard cap while being condensed.
    only_long = replace(fragment, alternatives=(long,))
    line = process._person_portrait_lines((only_long,), budget_chars=200)[0]
    assert long not in line
    assert short in line
    assert len(line.split('：',1)[1]) <= 200
    assert all(PromptProfile(i).includes('person') for i in range(1, 4))


def test_jev_hint_reaches_real_main_request_and_pending_writer(process):
    process.style_packs.set('stone', 'smith')
    process.zone_store.apply_edit('stone', (), append_blocks=('匠石正在现场。',))
    class Writer:
        requests = None
        def __init__(self):
            self.requests = []
        def generate(self, request):
            self.requests.append(request)
            return ModelResponse(model='writer')
    writer = Writer()
    process.write_zone = writer
    class Gate:
        name = 'gate'
        def generate(self, request):
            assert 'level' in request.system_extra
            return ModelResponse(model='gate', text='{"main_prompt_hint":{"level":3,"needs":[]},"items":[{"n":1,"to_jiangshi":"yes","relevance":"related"}]}')
    async def run():
        async def send(message):
            pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send, jev=VoiceJEV(Gate()), input_pause_s=.02)
        try:
            await c.accept(Transcript('匠石你好', 'A', 0, 2000, True))
            await asyncio.wait_for(c.queue.join(), 2)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            req = process.cognition.requests[-1]
            assert PERSONA_TOOL_NOTE not in req.persona_instruction
            assert '【此时的输入】' in req.persona_user_text
            rows = [r for r in process.repository.list_history('stone') if r.event_type == 'main_prompt_profile']
            assert rows[-1].content['level'] == 3
            assert rows[-1].content['scene_view'] == 1
        finally:
            await c.close()
    asyncio.run(run())
    assert writer.requests
    assert '一、四项主要输入' in writer.requests[-1].persona_instruction
    assert '【本次合批整理的输入输出】' in writer.requests[-1].persona_user_text


def test_batched_writer_uses_highest_pending_level(process):
    from types import SimpleNamespace
    process.pending_scene.append('stone', 'A', '简单问候', prompt_level=3)
    process.pending_scene.append('stone', 'B', '复杂任务', prompt_level=1)
    current = replace(process.assemble_current_state('stone', '你好'), prompt_level=1)
    payload = ('stone', SimpleNamespace(id='A'), current, None, None, (), None, 'smith', None)
    process._write_payloads['A'] = payload
    selected = []
    def capture(*args):
        selected.append(args[2].prompt_level)
        return True
    process._commit_zone = capture
    assert process._flush_pending_scene('stone')
    assert selected == [1]


def test_unexpected_tool_request_cannot_silently_override_jev_profile(process):
    from jshi.models.base import ToolUseIntent
    process.style_packs.set('stone', 'smith')
    process.zone_store.apply_edit('stone', (), append_blocks=('正在讨论天气。',))
    current = replace(process.assemble_current_state('stone', '天气如何？'), prompt_level=3)
    class Model:
        def __init__(self): self.requests=[]
        def generate(self, req):
            self.requests.append(req)
            if len(self.requests)==1:
                assert 'use_tool' not in req.persona_schema['properties']
                return ModelResponse(model='main',text='我核对一下。',tool_intent=ToolUseIntent('漏判的请求'))
            assert 'use_tool' in req.persona_schema['properties']
            return ModelResponse(model='main',text='这段不会再次播出。',tool_intent=ToolUseIntent('经完整认知核对的需求'))
    model=Model()
    process.cognition=model
    result=process._cognize_once(current,(),activity_id='profile-retry')
    assert len(model.requests)==1
    assert result.text=='我核对一下。'
    assert result.tool_intent is None
