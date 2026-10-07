from dataclasses import replace
from datetime import datetime, timezone
import json

from jshi.models import ModelResponse, ResponsePlan
from jshi.skill.zone import _to_write_response
from jshi.style.packs import write_schema_for, write_instruction_for
from jshi.style.zone import ZoneBlock, ZoneStore
from tests.test_voice import process


def test_nested_views_preserve_full_ids_and_chronology(tmp_path):
    store = ZoneStore(tmp_path / 'zone.json')
    store.boot('s', '我是匠石', [ZoneBlock('补充细节', tier=3),
                              ZoneBlock('重要承诺', tier=1), ZoneBlock('有用背景', tier=2)])
    short = store.render('s', max_tier=1)
    medium = store.render('s', max_tier=2)
    full = store.render('s')
    assert 'B3' in short and 'B2' not in short
    assert '重要承诺' in short and '有用背景' not in short
    assert '有用背景' in medium and '补充细节' not in medium
    assert full.index('补充细节') < full.index('重要承诺') < full.index('有用背景')
    assert all('我是匠石' in v for v in (short, medium, full))
    assert ZoneStore(store.path).render('s', max_tier=1) == short


def test_tier_refs_apply_before_deletion_renumbers_blocks(tmp_path):
    store = ZoneStore(tmp_path / 'zone.json')
    stamp = datetime(2026, 10, 5, tzinfo=timezone.utc)
    store.boot('s', '固定自述', [ZoneBlock('旧块', stamp), ZoneBlock('待更新', stamp),
                                ZoneBlock('我准备说：未播计划', stamp, 'speech_plan')])
    store.apply_edit('s', [
        {'op': 'del', 'id': 'B2'},
        {'op': 'mod', 'id': 'B3', 'text': '更新后的背景', 'tier': 2},
        {'op': 'tier', 'id': 'B4', 'tier': 3},
        {'op': 'mod', 'id': 'B4', 'text': '我已经说完'},
        {'op': 'add', 'text': '关键回忆', 'tier': 1},
        {'op': 'tier', 'id': 'A1', 'tier': 2},
    ], append_blocks_with_time=(ZoneBlock('本轮输入', origin='input'),))
    blocks = store.blocks('s')
    assert [b.tier for b in blocks] == [2, 3, 1, 2]
    assert blocks[0].at == stamp
    assert blocks[1].text == '我准备说：未播计划' and blocks[1].origin == 'speech_plan'
    assert store.body_chars('s') == len('固定自述') + sum(len(b.text) for b in blocks)


def test_legacy_and_invalid_tiers_are_retained_in_short_view(tmp_path):
    path = tmp_path / 'zone.json'
    path.write_text(json.dumps({'s': {'blocks': ['老记录', {'text': '无档位'},
                           {'text': '错误档位', 'tier': '3'}]}}, ensure_ascii=False), encoding='utf-8')
    store = ZoneStore(path)
    store.apply_edit('s', [{'op': 'tier', 'id': 'B1', 'tier': True},
                           {'op': 'tier', 'id': 'B2', 'tier': 4}])
    assert all(b.tier == 1 for b in store.blocks('s'))
    assert '老记录' in store.render('s', max_tier=1)


def test_main_selects_views_but_writer_reads_complete_scene(process):
    process.style_packs.set('stone', 'smith')
    process.zone_store.boot('stone', '固定自述', [ZoneBlock('关键待办', tier=1),
        ZoneBlock('接话背景', tier=2), ZoneBlock('可选细节', tier=3)])
    current = process.assemble_current_state('stone', '你好')
    for level, expected in [(1, 3), (2, 2), (3, 1)]:
        state = replace(current, prompt_level=level)
        main = process._persona_user_text(state, boot=False)
        writer = process._persona_user_text(state, boot=False, include_tool=False, live_zone=True)
        assert '关键待办' in main
        assert ('接话背景' in main) == (expected >= 2)
        assert ('可选细节' in main) == (expected == 3)
        assert '可选细节' in writer and '接话背景' in writer
        assert '[档位3]' in writer


def test_tier_metadata_passes_skill_and_program_append_without_duplicate_text(process):
    process.style_packs.set('stone', 'smith')
    process.zone_store.boot('stone', '固定自述', ['既有事实'])
    current = replace(process.assemble_current_state('stone', '本轮原话'),
        pending_write_text='合批时间线', pending_write_blocks=('本轮原话', '我准备说：回应'))
    parsed = _to_write_response({'edit': [{'op': 'tier', 'id': 'B2', 'tier': 2},
        {'op': 'tier', 'id': 'A1', 'tier': 1}, {'op': 'tier', 'id': 'A2', 'tier': 3}]}, 'test')
    reply = ModelResponse(model='test', response_plan=ResponsePlan(mode='wait'))
    process._apply_zone_edit('stone', reply, current, parsed)
    assert process.zone_store.get('stone') == ('既有事实', '本轮原话', '我准备说：回应')
    assert [b.tier for b in process.zone_store.blocks('stone')] == [2, 1, 3]
    schema = write_schema_for('smith')['properties']['edit']['items']['properties']
    assert 'tier' in schema['op']['enum'] and schema['tier']['enum'] == [1, 2, 3]
    assert '不输出长、中、短三份正文' in write_instruction_for('smith')
