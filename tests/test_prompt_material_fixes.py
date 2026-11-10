from tests.test_voice import process
from datetime import datetime, timezone
from jshi.style.zone import ZoneStore, ZoneBlock
from jshi.subject.process import _scene_input
from jshi.vision.store import description_current


def test_background_selection_keeps_original_indices_and_coverage(tmp_path):
    store = ZoneStore(tmp_path / 'zone.json')
    stamp = datetime(2026, 11, 2, tzinfo=timezone.utc)
    store.apply_edit('s', [{'op': 'del', 'id': 'A1'},
                           {'op': 'tier', 'id': 'A2', 'tier': 1}],
                     append_blocks_with_time=(ZoneBlock('嗯', stamp), ZoneBlock('取消订单的确认', stamp)),
                     covered_event_ids=('raw1', 'raw2'))
    assert [b.text for b in store.blocks('s')] == ['取消订单的确认']
    assert store.blocks('s')[0].tier == 1
    assert store.blocks('s')[0].at == stamp
    assert set(store.covered_event_ids['s']) == {'raw1', 'raw2'}


def test_scene_utterances_exclude_program_notes():
    assert _scene_input('1. lux：你好\n2. qf：好\n（程序说明）尚未说完\nJEV 初判：标注', 'lux') == 'lux：你好\nqf：好'
    assert _scene_input('1. 第一点是时间', 'lux') == 'lux说：“1. 第一点是时间”'


def test_visual_source_time_not_latest_frame_time():
    assert not description_current({'captured': 1000, 'described_at': 100}, 1000)
    assert description_current({'captured': 1000, 'described_at': 990}, 1000)
    assert not description_current({'captured': 1000}, 1000)


def test_preparation_supplies_confirmed_name_and_closes_uncertain_permission():
    from jshi.voice.jev_material import preparation_text
    import json
    snapshot = {'lines': [{'i': 1, 'p': 'P3', 'name': 'lux', 'text': '哈喽，匠石。', 'evidence_ref': 'I1'}],
                'scene': '', 'events': [], 'short_chat': {'allowed': True}, 'waiting': {'allowed': True},
                'availability': {}, 'environment': None, 'task_context': [], 'scope': {}}
    entry = {'items': [{'i': 1, 'person': 'P3', 'certainty': 'confirmed'}],
             'process_refs': ['I1'], 'pending_refs': [], 'interaction': 'direct'}
    text = preparation_text(snapshot, entry)
    assert 'lux' in text and '可用称呼' in text
    entry['items'][0]['certainty'] = 'tentative'
    entry['interaction'] = 'uncertain'
    text = preparation_text(snapshot, entry)
    assert 'lux' not in text
    assert '"allowed":true' not in text.replace(' ', '')


def test_direct_reply_restores_name_before_display_and_playback(tmp_path):
    import pytest
    from types import SimpleNamespace
    from jshi.app.voice import VoiceConversation
    seen = []
    async def start(items, *args):
        seen.append(items[0].text)
    async def emit(kind, **kwargs):
        seen.append(kwargs['text'])
    fake = SimpleNamespace(process=SimpleNamespace(_timings_path=tmp_path / 'timings.jsonl'),
                           subject_id='stone', voice_timings={}, debug_history=[], reply_input_ids={},
                           restore_codes=lambda text: text.replace('P3', 'lux'), _start_plan=start, emit=emit)
    utterance = SimpleNamespace(received_at_ms=None, input_id='input')
    envelope = SimpleNamespace(to_dict=lambda: {})
    task = VoiceConversation._direct_reply(fake, '哈喽，P3，我在呢。', 'lux-id', 1, envelope, [utterance], {})
    with pytest.raises(StopIteration):
        task.send(None)
    assert seen == ['哈喽，lux，我在呢。'] * 2
    assert fake.last_debug['response']['reply'] == seen[0]


def test_regrouped_duplicate_requires_exact_audio_boundaries_and_words():
    from jshi.voice.transcripts import equivalent_regrouping
    from jshi.voice.volc import Transcript
    from dataclasses import replace
    a = Transcript('我现在觉得。', '0', 100, 200, True)
    b = Transcript('是啊，无所谓。', '3', 200, 300, True)
    rows = {'a': {'transcript': a}, 'b': {'transcript': b}}
    merged = Transcript('我现在觉得是啊无所谓', '0', 100, 300, True)
    assert equivalent_regrouping(merged, rows) == ('a', 'b')
    assert not equivalent_regrouping(replace(merged, text='我现在觉得不是'), rows)
    assert not equivalent_regrouping(replace(merged, start_ms=99), rows)
    assert not equivalent_regrouping(merged, {**rows, 'b': {'transcript': b, 'audio_kind': 'separated'}})
    assert rows['b']['transcript'].track_id == '3'


def test_exact_duplicate_identity_repair_keeps_conflicts_for_validation():
    from jshi.voice.jev_serial import exact_identity_duplicates
    a = {'i': 1, 'person': 'P3', 'certainty': 'confirmed', 'why': '声音匹配'}
    data, changed = exact_identity_duplicates({'items': [a, dict(a)]})
    assert changed and data['items'] == [a]
    conflict = {**a, 'person': 'P2'}
    data, changed = exact_identity_duplicates({'items': [a, conflict]})
    assert not changed and len(data['items']) == 2


def test_anonymous_scope_and_legacy_header_projection():
    from jshi.core.scene_text import observation_text, scoped_voice_label
    raw = '[入口观察，input_id=raw-id，at=1793603637048.04] lux：再见'
    assert observation_text(raw) == '[记录于2026-11-02 15:13:57] lux：再见'
    assert raw.startswith('[入口观察')
    assert scoped_voice_label('待定声音 1', 'a') != scoped_voice_label('待定声音 1', 'b')
    assert scoped_voice_label('未命名访客 84', 'a') == '未命名访客 84'


def test_self_report_predicates_are_not_names():
    from jshi.voice.jev import explicit_name
    for phrase in ('我是这么觉得', '我是说', '我是做销售的', '我是工程师'):
        assert explicit_name(phrase) == ''
    for phrase, expected in [('我是lux', 'lux'), ('我叫一鸣', '一鸣'), ('我是小林', '小林')]:
        assert explicit_name(phrase) == expected


def test_writer_drains_bounded_batches_and_retains_failed_rows(process):
    from jshi.models import ModelResponse
    process.style_packs.set('stone', 'smith')
    process.experience('stone', '开始', object_ref='user', defer_write=True)
    # Replace pending rows with large atomic events using the existing writer payload.
    original = process.pending_scene.snapshot('stone')[0]
    process.pending_scene.complete([original['id']])
    for i in range(3):
        process.pending_scene.append('stone', f'large-{i}', '材料' * 3500)
    counts = []
    def commit(sid, activity, current, *args):
        counts.append((len(current.pending_write_ids), sum(map(len, current.pending_write_blocks))))
        process.pending_scene.complete(current.pending_write_ids)
        return True
    process._commit_zone = commit
    assert process._flush_pending_scene('stone')
    assert counts == [(1, 7000)] * 3
    assert not process.pending_scene.snapshot('stone')
    process.pending_scene.append('stone', 'retry', '保留')
    process._commit_zone = lambda *args: False
    assert not process._flush_pending_scene('stone')
    assert process.pending_scene.snapshot('stone')[0]['id'] == 'retry'


def test_cosmetic_revision_does_not_hide_meaningful_punctuation():
    from jshi.voice.transcripts import cosmetic_revision
    assert cosmetic_revision('还好吧。', '还好吧')
    assert cosmetic_revision(' 还 好 吧 ', '还好吧。')
    assert not cosmetic_revision('可以？', '可以。')
    assert not cosmetic_revision('不要，停', '不要停')


def test_current_frame_can_outlive_its_description():
    from jshi.vision.store import image_current, description_current
    snapshot = {'captured': 1000, 'described_at': 100}
    assert image_current(snapshot, 1001)
    assert not description_current(snapshot, 1001)
    assert not image_current(snapshot, 1061)


def test_structured_scene_uses_raw_fields_not_rendered_program_notes():
    from types import SimpleNamespace
    from jshi.subject.process import _scene_turn
    current = SimpleNamespace(audio_delivery=True,
        input_text='1. lux：错误包装\n（程序说明）分批',
        input_items=({'display': 'lux', 'actor_id': 'person', 'text': '1. 这是原话里的编号'},))
    assert _scene_turn(current, '其他人') == 'lux：1. 这是原话里的编号'


def test_media_aliases_are_local_consistent_and_preserve_sources():
    from jshi.core.main_input import make_input, render_inputs
    a = {'kind': 'audio', 'reference': 'session:long-id:1000-2000'}
    b = {'kind': 'image', 'reference': 'long-frame-id'}
    items = (make_input('a', 'p', 'lux', '一句', parts=(a, b)),
             make_input('b', 'p', 'lux', '两句', parts=(a,)))
    text = render_inputs(items)
    assert 'session:' not in text and 'long-frame-id' not in text
    assert text.count('媒体1') == 2 and text.count('媒体2') == 1
    assert items[0]['parts'][0]['reference'] == a['reference']


def test_entry_uncertainty_survives_merged_batches_without_alias_collisions():
    from jshi.core.main_input import entry_assessments
    calls = [{'input_ids': ['a', 'removed'], 'interaction': 'uncertain',
              'decision_reason': 'I1意思含糊；I2未完；E3仅为自身插话'},
             {'input_ids': ['b'], 'interaction': 'direct', 'decision_reason': 'I1明确呼唤'},
             {'input_ids': ['other'], 'interaction': 'direct', 'decision_reason': '其他输入'}]
    text = entry_assessments(calls, ['b', 'a'])
    assert '第2条：是否对匠石说尚不确定' in text
    assert '本轮第2条意思含糊' in text and '本轮第1条明确呼唤' in text
    assert 'I1' not in text and 'E3' not in text and '其他输入' not in text
    assert '可以不回应' in text
    assert entry_assessments(calls, ['unrelated']) == ''


def test_interaction_origin_preserves_age_without_program_ids():
    from jshi.voice.jev_material import build_snapshot
    payload = {'input': {'lines': []}, 'now': 100000}
    origin = {'text': '你好', 'basis': '有效面对面招呼', 'at_ms': 97500, 'input_id': 'private-long-id'}
    state = build_snapshot(payload, [], {'interaction_origin': origin})['state']
    assert state['interaction_origin'] == {'text': '你好', 'basis': '有效面对面招呼', 'age_seconds': 2}
    assert origin['input_id'] == 'private-long-id'


def test_visual_description_alone_does_not_establish_human_contact():
    from jshi.voice.jev_serial import voice_interaction_origin
    assert voice_interaction_origin({'lines': [{'source': 'visual_environment', 'text': '有人看着匠石的屏幕'}],
        'availability': {'image_available': True}}) is None
    assert voice_interaction_origin({'lines': [{'source': 'voice', 'text': '你好'}],
        'environment': {'description': '近处一人面向设备'}, 'availability': {'image_available': True}})
