"""Scene evidence reaches existing stages without inventing facts or calls."""
import pytest

from jshi.core.scene_context import scene_cautions, SCENE_GROUNDING_RULE
from jshi.core.scene_updates import shared_update_context
from jshi.core.main_input import entry_assessments
from jshi.voice.jev_material import build_snapshot, entry_text, preparation_text, support_for_main


def snapshot(events, **extra):
    line = {'i': 1, 'text': '你来看看', 'evidence_ref': 'I1', 'source': 'voice'}
    payload = {'input': {'lines': [line], 'unwritten': events}, 'scene': '两人在讨论操作', **extra}
    return build_snapshot(payload, [line], {})


def test_event_budget_keeps_nearest_human_exchange_and_scoped_correction():
    events = [{'at': n, 'ref': f'E{n}', 'text': str(n) + '旧背景' * 400} for n in range(12)]
    events += [{'at': 20, 'ref': 'E20', 'p': 'P1', 'text': '你刚才关掉了没有？'},
               {'at': 21, 'ref': 'E21', 'p': 'P2', 'text': '还没有，我现在去。'},
               {'at': 22, 'ref': 'E22', 'kind': 'understanding_update',
                'text': '这段是P1与P2互相询问，匠石此前插话没有得到承接'}]
    data = snapshot(events)
    refs = [row['ref'] for row in data['events']]
    assert all(ref in refs for ref in ['E20', 'E21', 'E22'])
    assert 'E0' not in refs
    assert data['scope']['unprovided_events'] > 0
    assert [r['at'] for r in data['events']] == sorted(r['at'] for r in data['events'])
    user = entry_text(data)
    assert user.count(events[-1]['text']) == 1
    assert user.index('现场认识更新') < user.index('当前现场·JEV2')
    prepared = preparation_text(data, {'process_refs': ['I1'], 'pending_refs': [], 'items': [],
                                     'interaction': 'uncertain'}, main_only=True)
    assert prepared.count(events[-1]['text']) == 1
    assert prepared.index('本次处理原话') < prepared.index('入口结论')


def test_observation_and_conditional_rules_are_not_duplicated_or_fabricated():
    data = snapshot([], environment={'description': '两人在餐桌旁交谈', 'status': 'description_stale'})
    text = entry_text(data)
    assert text.count('两人在餐桌旁交谈') == 1
    assert '视觉描述已过期' in text
    assert not scene_cautions([{'text': '你好'}])
    assert '叠音' in scene_cautions([{'overlap': True}])
    assert '转写修订' in scene_cautions([{'asr_previous': ['旧文']}])


def test_accumulated_corrections_do_not_evict_current_human_context():
    corrections = [{'at': n, 'ref': f'E{n}', 'kind': 'understanding_update',
                    'text': str(n) + '有范围的旧认识修正' * 60} for n in range(20)]
    human = {'at': 30, 'ref': 'E30', 'p': 'P2', 'text': '我接着回答刚才她的问题'}
    data = snapshot([*corrections, human])
    assert any(row['ref'] == 'E19' for row in data['events'])
    assert any(row['ref'] == 'E30' for row in data['events'])
    assert data['scope']['unprovided_events'] > 0


def test_shared_update_age_and_scope_survive_without_future_or_internal_ids():
    update = {'scope': '甲乙这段设备讨论', 'understanding': '双方彼此问答，未向匠石提问',
              'basis': '此前入口误认为direct；新问答明确延续甲乙的话'}
    rows = [{'id': 'internal-activity-id', 'kind': 'understanding_update', 'at': 1000, 'update': update},
            {'kind': 'understanding_update', 'at': 999999, 'update': {**update, 'scope': '未来材料'}}]
    first = shared_update_context(rows, 31000)
    later = shared_update_context(rows, 91000)
    assert '30秒前更新' in first and '1分钟前更新' in later
    assert update['scope'] in first and '未来材料' not in first
    assert 'internal-activity-id' not in first
    assert not shared_update_context([], 31000)
    assert '更新时间未知' in shared_update_context([{**rows[0], 'at': None}], 31000)


def test_late_transcript_evidence_reaches_main_with_merged_input_number():
    material = {'lines': [{'i': 1, 'evidence_ref': 'I1',
                          'audio_delivery': {'recognition_delay_seconds': 117}}], 'evidence': []}
    support = support_for_main(material)
    rendered = entry_assessments([{'input_ids': ['second'], 'interaction': 'direct',
        'decision_reason': 'I1明确呼唤', 'entry_support': support}], ['first', 'second'])
    assert '第2条补充证据' in rendered and '117秒后才到达' in rendered
    assert rendered.index('原始材料的依据与缺口') < rendered.index('本轮入口判断')
    assert '不是本句时长' in rendered
    assert not support_for_main({'lines': [{'i': 1, 'evidence_ref': 'I1'}], 'evidence': []})


@pytest.mark.parametrize('pack', ['wood', 'smith'])
def test_main_reads_shared_correction_even_after_writer_consumed_pending(tmp_path, pack):
    from tests.test_write_zone_split import runtime, ReplyModel
    from jshi.models.prompt import build_user

    class Capture(ReplyModel):
        def generate(self, request):
            self.requests = [*getattr(self, 'requests', []), request]
            return super().generate(request)

    model = Capture()
    process, _ = runtime(tmp_path, model, None)
    process.style_packs.set('stone', pack)
    if pack == 'smith':
        process.zone_store.boot('stone', value='我是匠石。', scene=['甲乙正在讨论设备。'])
    update = {'scope': '甲乙的当前设备讨论', 'understanding': '此前误接；现在是双方互相询问',
              'basis': '甲明确问乙，乙回答甲，未承接匠石上一句'}
    process.pending_scene.append_updates('stone', 'prior-correction', [update])
    process.pending_scene.complete([row['id'] for row in process.pending_scene.snapshot('stone')])
    process.experience('stone', '那我来试一下', object_ref='user', defer_write=True)
    assert len(model.requests) == 1
    text = build_user(model.requests[0])
    assert '【共享现场认识更新】' in text and update['basis'] in text
    assert 'prior-correction' not in text


def test_shared_rule_present_once_in_split_combined_and_main():
    from jshi.voice.jev_prompts import ENTRY_INSTRUCTION
    from jshi.voice.jev_serial import combined_instruction, split_instruction
    from jshi.core.scene_updates import SCENE_UPDATE_NOTE
    for text in [ENTRY_INSTRUCTION, split_instruction(), combined_instruction(), SCENE_UPDATE_NOTE]:
        assert text.count(SCENE_GROUNDING_RULE) == 1


def test_recent_original_exchange_reaches_main_with_seconds_and_delivery_status():
    from jshi.voice.jev_material import recent_context_for_main
    data = {'lines': [{'i': 1, 'p': 'P1', 'text': '你还在吗？', 'at': 0, 'evidence_ref': 'I1'}],
            'evidence': [], 'people': {'P1': '甲'},
            'state': {'event_time_unit': 'seconds'},
            'events': [{'p': 'P1', 'text': '匠石。', 'at': -2.3, 'basis': '收到的原话',
                        'identity': {'person': 'P1', 'certainty': 'possible'}, 'id': 'private-id'},
                       {'p': '匠石', 'text': '我在。', 'at': -1, 'basis': '生成，未确认播放'},
                       {'p': 'P1', 'text': '你还在吗？', 'at': 0},
                       {'p': 'P1', 'text': '未来发言', 'at': 5}]}
    support = support_for_main(data)
    text = entry_assessments([{'input_ids': ['a'], 'interaction': 'direct',
                              'entry_support': support, 'decision_reason': '自然续谈'}], ['a'])
    assert '判断前约2秒，甲' in text and '人物可靠性：possible' in text
    assert '生成，未确认播放' in text and 'private-id' not in text
    assert '未来发言' not in text and '你还在吗' not in text
    assert text.index('入口所见最近原始接续') < text.index('本轮入口判断')
    data['state'] = {}
    assert '事件时间未知' in recent_context_for_main(data)


def test_recent_projection_keeps_whole_utterances_and_old_units_explicit():
    from jshi.voice.jev_material import recent_context_for_main
    data = {'state': {'time_basis': '事件与环境时间均为相对now的整数毫秒'},
            'events': [{'p': 'P1', 'text': '早一条', 'at': -4000},
                       {'p': 'P1', 'text': '很长但不能截断的原话' * 400, 'at': -3000},
                       {'p': 'P2', 'text': '后一句含必要否定：不是问你', 'at': -2256}]}
    text = recent_context_for_main(data, limit=1, cap=120)
    assert '判断前约2秒' in text and '不是问你' in text
    assert '早一条' not in text and '很长' not in text
    repeated = {'state': {'event_time_unit': 'seconds'},
                'lines': [{'p': 'P3', 'text': '匠石', 'at': 0}],
                'events': [{'p': 'P3', 'text': '匠石', 'at': -5}]}
    assert '入口临时对象P3' in recent_context_for_main(repeated)
    assert '匠石' in recent_context_for_main(repeated)


def test_direction_marks_are_revisable_in_both_directions():
    from jshi.core.main_input import make_input, render_inputs
    text = render_inputs([make_input('a', 'person', '甲', '匠石', direction='yes'),
                          make_input('b', 'person', '甲', '后来转向你', direction='no')])
    assert '入口初判对匠石说' in text and '须核对原话' in text
    assert '入口初判为背景，可由本轮新证据修正' in text


def test_recent_evidence_budget_applies_across_merged_entry_calls():
    calls = []
    for n in range(5):
        row = f'入口判断前约2秒，入口临时对象P1（仅该入口内的代号）：批次{n}原话\n' + '完整证据' * 105 + '不是问你'
        calls.append({'input_ids': [str(n)], 'interaction': 'direct',
                      'entry_support': [{'n': 1, 'notes': [], 'recent_events': [row]}]})
    text = entry_assessments(calls, [str(n) for n in range(5)])
    recent = text.split('【入口所见最近原始接续】\n')[1].split('\n这是原始事件节选')[0]
    assert len(recent) <= 1200
    assert '批次4原话' in recent and '批次3原话' in recent and '批次0原话' not in recent
    assert recent.count('不是问你') == 2  # Whole multiline utterances, no lost qualifier.
    assert '本轮第4条的入口所见' in recent and '本轮第5条的入口所见' in recent


def test_recent_evidence_zero_seconds_and_unknown_remain_distinct():
    from jshi.voice.jev_material import recent_context_for_main
    text = recent_context_for_main({'state': {'event_time_unit': 'seconds'},
        'events': [{'p': 'P1', 'text': '同一时点', 'at': 0},
                   {'p': 'P2', 'text': '时间缺失', 'at': None}]})
    assert '入口判断前约0秒' in text and '事件时间未知' in text


@pytest.mark.parametrize('other_id,other_text,other_person,expected', [
    ('original-input-0123456789abcdef', '匠石。', '声音归属待定', 1),
    ('different-input-fedcba9876543210', '匠石。', 'unknown', 2),
    ('original-input-0123456789abcdef', '降十。', 'unknown', 2),
    ('', '匠石。', 'unknown', 1),
])
def test_real_jev_material_deduplicates_source_mirrors_without_merging_repetitions(
        other_id, other_text, other_person, expected):
    import json
    import re
    from copy import deepcopy
    from jshi.models import ModelResponse
    from jshi.voice.jev import VoiceJEV

    class Model:
        name = 'capture-entry'
        def generate(self, request):
            self.request = request
            return ModelResponse(model=self.name, text=json.dumps({
                'interaction': 'none', 'items': [{'i': 1, 'person': 'unknown',
                'certainty': 'unknown', 'why': '未确认'}], 'utterance_complete': 'complete',
                'control': 'keep', 'route': 'background', 'process_refs': [], 'pending_refs': [],
                'decision_reason': '背景观察', 'decision_refs': ['I1'], 'front': None}))

    now = 1794316000000
    context = [{'input_id': 'original-input-0123456789abcdef', 'ref': 'E1', 'p': 'unknown',
                'text': '匠石。', 'at': now-2000, 'basis': 'unassigned_audio'}]
    conversation = [{'who': other_person, 'text': other_text, 'at': now-2000,
                     'basis': '收到的原话', **({'input_id': other_id} if other_id else {})}]
    original = deepcopy((context, conversation))
    model = Model()
    gate = VoiceJEV(model, mode='split', fallback_models=[])
    gate.decide_batch('stone', [{'n': 1, 'who': 'unknown', 'text': '你还在吗？', 'at': now}],
                      context, {'now': now, 'conversation': conversation})
    user = model.request.input_text
    raw = re.search(r'【尚未编排的新事件】\n(.*?)(?=\n\n【|$)', user, re.S)[1]
    events = json.loads(raw)
    assert len(events) == expected
    assert 'original-input-' not in user and 'different-input-' not in user and 'input_id' not in user
    assert (context, conversation) == original
    if other_text != '匠石。':
        assert {row['text'] for row in events} == {'匠石。', other_text}


def test_dialogue_source_ids_are_opt_in_and_do_not_shrink_material_window(tmp_path):
    from jshi.voice.dialogue import VoiceDialogue
    dialogue = VoiceDialogue(tmp_path/'dialogue.json')
    for n in range(20):
        dialogue.input('stone', str(n) + 'x'*200, '原始内容'*20, '归属未明', at=1000+n)
    plain = dialogue.recent('stone', cutoff=2000)
    host = dialogue.recent('stone', cutoff=2000, include_ids=True)
    assert len(plain) == len(host)
    assert all('input_id' not in row for row in plain)
    assert all(row.get('input_id') for row in host)
    assert [{k:v for k,v in row.items() if k != 'input_id'} for row in host] == plain
