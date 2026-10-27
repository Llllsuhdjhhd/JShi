import asyncio
import copy
import json
from dataclasses import asdict

import pytest

from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV
from jshi.voice.jev_input import compact_jev_input
from jshi.voice.jev_scene import JEVSceneStore, JEVSceneWriter, recent_material
from jshi.voice.scene_material import recent_scene_blocks
from tests.test_voice import process, FakeCloud


class Gate:
    def __init__(self, route='main', control='keep', complete='complete'):
        self.route, self.control, self.complete = route, control, complete

    def generate(self, request):
        self.request = request
        data = json.loads(request.input_text)
        return ModelResponse(model='fake', text=json.dumps({
            'items': [{'i': r['i'], 'person': 'unknown', 'certainty': 'unknown',
                       'why': '姓名未确认，不妨碍判断接话关系'} for r in data['input']['lines']],
            'level': 3, 'recall_memory': False, 'main_image': 'omit',
            'utterance_complete': self.complete, 'control': self.control, 'route': self.route,
            'decision_reason': '可能在调侃上一句追问，静默交主认知判断',
            'decision_refs': ['I1'] + (['C1'] if data['conversation'] else []),
            'reply': '', 'waiting_reply': ''}, ensure_ascii=False))


@pytest.mark.parametrize('state', ['completed', 'playing', 'thinking'])
def test_keep_task_can_still_route_uncertain_identity_to_main(state):
    gate = Gate()
    result = VoiceJEV(gate, mode='legacy').decide_batch('stone', [{'n': 1, 'text': '对钱很感兴趣嘛。哼。'}], [],
        {'state': state, 'conversation': [{'who': '匠石', 'text': '十五万指哪一笔？', 'at': 1000}]})
    assert result.action == 'respond' and result.route == 'main' and result.control == 'keep'
    assert result.needs_main_review and result.items[0].speaker_pick == 'unknown'
    assert result.front_choice == 'main_silent' and result.decision_refs == ('I1', 'C1')


@pytest.mark.parametrize('route', ['background', 'wait'])
def test_background_and_wait_cannot_cancel_current_task(route):
    result = VoiceJEV(Gate(route, 'redirect'), mode='legacy').decide_batch('stone', [{'n': 1, 'text': '我把葱吃掉'}], [], {'state': 'playing'})
    assert result.control == 'keep' and result.action == 'ignore' and result.route == route
    assert result.front_choice == route


def test_unfinished_expression_still_waits_and_stop_still_stops():
    result = VoiceJEV(Gate(complete='incomplete'), mode='legacy').decide_batch('stone', [{'n': 1, 'text': '如果明天'}], [], {})
    assert result.route == 'wait' and result.action == 'ignore'
    result = VoiceJEV(Gate(control='stop', complete='incomplete'), mode='legacy').decide_batch('stone', [{'n': 1, 'text': '先停，我要'}], [], {})
    assert result.control == 'stop' and result.action == 'stop'


def test_dialogue_survives_large_repeated_voice_evidence(tmp_path):
    store = JEVSceneStore(tmp_path / 'scene.json')
    for i, text in enumerate(['哈喽', '哈喽，我在呢', '十五万指哪笔', '对钱很感兴趣嘛', '我把葱吃掉']):
        store.append(f'long-source-{i}', {'text': text, 'at': 1000+i,
            'short_voice_hint': {'candidates': ['duplicated evidence' * 500]},
            'judgment': {'person': 'P1', 'certainty': '可能', 'why': '暂归'},
            'candidates': [{'p': 'P1', 'similarity': .29, 'source': 'voiceprint'}]})
    snap = store.snapshot()
    rows, _ = recent_material(snap, '')
    assert [r['text'] for r in rows] == ['哈喽', '哈喽，我在呢', '十五万指哪笔', '对钱很感兴趣嘛', '我把葱吃掉']
    assert not snap['material_window']['deferred_input_ids']
    assert 'duplicated evidence' not in json.dumps(rows)
    assert store.commit(snap, '饭桌旁聊与对匠石的调侃并行。')
    assert store.snapshot()['events'] == []


def test_correction_preserves_scene_and_exact_event_reference(tmp_path):
    store = JEVSceneStore(tmp_path / 'scene.json')
    store.append('long-original-id', {'text': '对钱很感兴趣嘛', 'at': 1000,
        'judgment': {'person': 'P2', 'certainty': '可能'}})
    old = store.snapshot()
    store.commit(old, '大家在吃饭，匠石刚追问钱。')
    stale = store.snapshot()
    store.correct({'text': '仅本句确认qf', 'basis': 'manual_annotation',
        'corrected_events': [{'ref': 'long-original-id', 'text': '对钱很感兴趣嘛', 'at': 1000, 'person': 'P1'}]})
    assert '大家在吃饭' in store.snapshot()['scene']
    assert not store.commit(stale, '旧判断晚回')
    snap = JEVSceneStore(store.path).snapshot()
    rows, _ = recent_material(snap, '')
    corrected = rows[0]['corrected_events'][0]
    assert corrected['ref'] == snap['event_refs']['long-original-id'] == 'E1'
    assert corrected['person'] == 'P1'
    assert next(r for r in snap['recent_context'] if r['ref'] == 'E1')['identity']['person'] == 'P1'
    packed = compact_jev_input({'input_output': rows, 'identity_evidence': snap['identity_evidence'], 'now': 2000})
    assert packed['input_output'][0]['corrected_events'][0]['ref'] == 'E1'
    assert 'long-original-id' not in json.dumps(packed)


def test_stale_description_is_removed_without_mutating_source_or_refreshing_unknown_time():
    for described in [1, 0, None]:
        original = {'now': 200_000, 'environment': {'captured': 199, 'described_at': described,
                    'description': '很久以前坐在镜头前的人', 'frame': 'old-frame', 'image_available': True}}
        before = copy.deepcopy(original)
        packed = compact_jev_input(original)
        assert original == before
        assert 'description' not in packed['environment']
        assert packed['environment']['status'] in {'description_stale', 'description_time_unknown'}
        nested = compact_jev_input({'now': original['now'], 'input_output': [before]})
        assert 'description' not in nested['input_output'][0]['environment']


def test_invalid_routing_reference_preserves_identity_and_does_not_interrupt():
    class Invalid(Gate):
        def generate(self, request):
            raw = json.loads(super().generate(request).text)
            raw['decision_refs'] = ['C999']
            return ModelResponse(model='fake', text=json.dumps(raw))
    result = VoiceJEV(Invalid(), mode='legacy').decide_batch('stone', [{'n':1, 'text':'你说呢'}], [], {'state':'playing'})
    assert result.judged and result.items[0].speaker_pick == 'unknown'
    assert result.control == 'keep' and result.route == 'background' and result.routing_error


def test_many_selected_annotations_are_batched_without_discarding_scene(tmp_path):
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('start', {'text':'大家在吃饭'})
    store.commit(store.snapshot(), '大家在吃饭。')
    changes = [{'ref':f'raw-{i}', 'text':f'第{i}句原话', 'person':'P1', 'at':None} for i in range(32)]
    store.correct({'text':'只纠正所选32句', 'corrected_events':changes})
    snapshot = store.snapshot()
    assert '大家在吃饭' in snapshot['scene']
    assert len(snapshot['events']) == 6
    rows, _ = recent_material(snapshot, '')
    assert not snapshot['material_window']['archived_input_ids']
    assert rows[0]['corrected_events'][0]['at'] is None
    assert len({r['ref'] for row in rows for r in row['corrected_events']}) == sum(len(row['corrected_events']) for row in rows)


def test_old_rules_and_unanswered_tasks_do_not_crowd_current_reference():
    reference, omitted = recent_scene_blocks('B1[2周前] 没点名别说话\nB2[半天前] 会诊资料未到\nB3[1分钟前] 匠石刚追问十五万')
    assert reference == 'B3[1分钟前] 匠石刚追问十五万' and omitted == 2


@pytest.mark.parametrize('playing', [False, True])
def test_voice_keep_main_reaches_cognition_without_cancelling_task(process, playing):
    from jshi.app.voice import VoiceConversation
    from jshi.voice.volc import Transcript

    async def run():
        async def send(message):
            pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send, jev=VoiceJEV(Gate(), mode='legacy'), input_pause_s=.01)
        try:
            old = c.delivery.begin('旧任务仍在播放。', 'lux-id') if playing else None
            if old:
                old.state = 'playing'
            epoch = c.epoch
            await c.accept(Transcript('对钱很感兴趣嘛。哼。', 'A', 0, 3000, True))
            await asyncio.wait_for(c.queue.join(), 3)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            assert process.cognition.requests
            assert c.epoch == epoch
            if old:
                assert c.delivery.current.reply_id == old.reply_id
                assert c.delivery.current.state == 'playing'
            logs = [r for r in process.repository.list_history('stone') if r.event_type == 'voice_jev_diagnostics']
            assert logs[-1].content['route'] == 'main'
            assert logs[-1].content['decision_refs'] == ['I1']
        finally:
            await c.close()
    asyncio.run(run())
