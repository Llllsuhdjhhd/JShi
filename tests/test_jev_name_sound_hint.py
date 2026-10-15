"""JEV name_sound_hint + thin_batch seconds (no network; stub captures payload)."""
from __future__ import annotations

import json

from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV
from jshi.voice.jev_prompts import ENTRY_INSTRUCTION, SINGLE_INSTRUCTION
from jshi.voice.name_sound import name_sound_hint, weak_hint_yields_to_scene

NAMES = ['匠石', '匠石啊', '匠石同学', '将士']

_YIELD_MARKERS = (
    'name_sound_hint是程序给出的',
    '只是证据不是证明',
    '必须让位于该现场证据',
    '不要凭同音猜测',
)


class CaptureModel:
    """Stub model that records the gate request and returns a fixed JSON body."""

    def __init__(self, text: str):
        self.text = text
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return ModelResponse(model='stub', text=self.text)


def _single_ok(action='respond'):
    return json.dumps({'action': action, 'reason': 'stub', 'claimed_name': ''}, ensure_ascii=False)


def _batch_ok():
    return json.dumps({
        'items': [{'i': 1, 'person': 'unknown', 'certainty': 'unknown', 'why': 'stub identity'}],
        'level': 3, 'recall_memory': False, 'main_image': 'omit', 'route': 'main', 'reply': '',
    }, ensure_ascii=False)


def test_name_sound_hint_matched_fractions():
    assert name_sound_hint('将将，你觉得呢？', NAMES) == {
        'token': '将将', 'target': '匠石', 'matched': '1/2', 'position': 'vocative'}
    assert name_sound_hint('将室…', NAMES) == {
        'token': '将室', 'target': '匠石', 'matched': '2/2', 'position': 'vocative'}


def test_hello_prefix_then_cjk_vocative():
    """Leading Latin is ignored; CJK vocative ≤4 still matches (e.g. HELLO 士 → 匚石 1/2)."""
    names = ['匚石', '匚石啊', '匚石同学']
    assert name_sound_hint('HELLO 士', names) == {
        'token': '士', 'target': '匚石', 'matched': '1/2', 'position': 'vocative'}
    assert name_sound_hint('HELLO 士，…', names) == {
        'token': '士', 'target': '匚石', 'matched': '1/2', 'position': 'vocative'}


def test_no_hint_for_unrelated_vocatives_and_latin():
    for text in ('連軍啊', '小左', 'Ross', 'Phoebe'):
        assert name_sound_hint(text, NAMES) is None
    # Pure Latin still yields no hint even when 匚石 is on the names list.
    assert name_sound_hint('Ross', ['匚石']) is None
    assert name_sound_hint('Phoebe', ['匚石']) is None


def test_decide_payload_includes_hint_and_instruction_rule():
    model = CaptureModel(_single_ok())
    VoiceJEV(model).decide(
        'stone', '将将，你觉得呢？', {},
        {'state': 'completed', 'names_for_jiangshi': NAMES})
    assert model.requests
    payload = json.loads(model.requests[0].input_text)
    assert payload['name_sound_hint']['matched'] == '1/2'
    assert payload['name_sound_hint']['token'] == '将将'
    assert payload['name_sound_hint']['position'] == 'vocative'
    for marker in _YIELD_MARKERS:
        assert marker in model.requests[0].system_extra
        assert marker in SINGLE_INSTRUCTION


def test_jiangjiang_weak_hint_yields_to_scene_presence_not_respond():
    """Weak 1/2 hint + scene person 江江 → instruction/payload carry yield; stub must not respond."""
    scene = '【人物与交互】现场有人叫江江，正在吃饭；旁人问他吃了没。'
    captured = {}

    class YieldStub:
        def generate(self, request):
            captured['system'] = request.system_extra
            captured['payload'] = json.loads(request.input_text)
            hint = captured['payload'].get('name_sound_hint')
            scene_text = (captured['payload'].get('delivery') or {}).get('scene') or ''
            # Rule/stub: weak hint that yields to a same-named person on site → not respond.
            if weak_hint_yields_to_scene(hint, scene_text):
                return ModelResponse(model='stub', text=_single_ok('ignore'))
            return ModelResponse(model='stub', text=_single_ok('respond'))

    decision = VoiceJEV(YieldStub()).decide(
        'stone', '江江，你吃了吗', {},
        {'state': 'completed', 'names_for_jiangshi': NAMES, 'scene': scene})
    hint = captured['payload']['name_sound_hint']
    assert hint['matched'] == '1/2' and hint['token'] == '江江'
    assert '江江' in (captured['payload'].get('delivery') or {}).get('scene', '')
    assert weak_hint_yields_to_scene(hint, scene) is True
    for marker in _YIELD_MARKERS:
        assert marker in captured['system']
    assert decision.action != 'respond'
    assert decision.action == 'ignore'


def test_thin_batch_line_includes_seconds_and_hint():
    model = CaptureModel(_batch_ok())
    batch = [{'n': 1, 'text': '将室，听得到吗？', 'who': 'unknown', 'at': 1000,
              'seconds': 1.25, 'voice_evidence': {'strength': 'unavailable', 'basis': '无声纹'}}]
    VoiceJEV(model).decide_batch(
        'stone', batch, [],
        {'state': 'completed', 'names_for_jiangshi': NAMES, 'short_chat': {'allowed': False}})
    assert model.requests
    payload = json.loads(model.requests[0].input_text)
    line = payload['input']['lines'][0]
    assert line['seconds'] == 1.25
    assert line['name_sound_hint']['matched'] == '2/2'
    assert line['name_sound_hint']['token'] == '将室'
    assert 'name_sound_hint' in model.requests[0].system_extra
    assert '必须让位于该现场证据' in ENTRY_INSTRUCTION
