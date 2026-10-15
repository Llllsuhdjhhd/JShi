"""JEV name_sound_hint + thin_batch seconds (no network; stub captures payload)."""
from __future__ import annotations

import json
import re

from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV
from jshi.voice.jev_prompts import ENTRY_INSTRUCTION, SINGLE_INSTRUCTION
from jshi.voice.name_sound import name_sound_hint, weak_hint_yields_to_scene

NAMES = ['匠石', '匠石啊', '匠石同学', '将士']

# Instruction contract phrases added for name_sound_hint / meeting / weak-hint rules.
_SINGLE_RULES = (
    '场景写「助手」不等于默认接令',
    '会议场景下的祈使、翻页、换方案等方案陈述应ignore',
    '若有弱提示或明确叫名，本条不盖掉接话',
    '弱线索（k<n，如1/2）在句首称呼＋面向听者的疑问或请求（呢、吗、你觉得等）时倾向respond',
    '仍必须让位为ignore',
)
_ENTRY_RULES = (
    '场景写「助手」不等于默认接令',
    '会议场景下的祈使、翻页、换方案等方案陈述不当成对匠石下指令',
    '若有弱提示或明确叫名，本条不盖掉接话判断',
    '弱线索（k<n，如1/2）在句首称呼＋面向听者的疑问或请求（呢、吗、你觉得等）时倾向视为对匠石接话',
    '仍必须让位于该现场证据',
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


def _scripted_single_action(payload: dict) -> str:
    """Deterministic stand-in for the new SINGLE rules (tests the contract, not the LLM)."""
    utterance = str(payload.get('utterance') or '')
    delivery = payload.get('delivery') or {}
    names = list(delivery.get('names_for_jiangshi') or [])
    hint = payload.get('name_sound_hint')
    scene = str(delivery.get('scene') or '')
    if weak_hint_yields_to_scene(hint, scene):
        return 'ignore'
    if hint and isinstance(hint.get('matched'), str) and '/' in hint['matched']:
        try:
            k_s, n_s = hint['matched'].split('/', 1)
            k, n = int(k_s), int(n_s)
        except ValueError:
            k, n = 0, 0
        if 0 < k < n and re.search(r'呢|吗|你觉得', utterance):
            return 'respond'
    named = any(n and n in utterance for n in names)
    meeting_imperative = bool(re.search(r'翻页|下一页|换方案|换个方案|方案改', utterance))
    if hint is None and not named and meeting_imperative:
        return 'ignore'
    return 'respond'


class RuleContractStub:
    """Stub that returns action from the instruction-contract helper after capturing the request."""

    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        payload = json.loads(request.input_text)
        return ModelResponse(model='stub', text=_single_ok(_scripted_single_action(payload)))


def test_name_sound_hint_matched_fractions():
    assert name_sound_hint('将将，你觉得呢？', NAMES) == {
        'token': '将将', 'target': '匠石', 'matched': '1/2', 'position': 'vocative'}
    assert name_sound_hint('将室…', NAMES) == {
        'token': '将室', 'target': '匠石', 'matched': '2/2', 'position': 'vocative'}


def test_hello_prefix_then_cjk_vocative():
    """Leading Latin is ignored; CJK vocative ≤4 still matches (e.g. HELLO 士 → 匠石 1/2)."""
    names = ['匠石', '匠石啊', '匠石同学']
    assert name_sound_hint('HELLO 士', names) == {
        'token': '士', 'target': '匠石', 'matched': '1/2', 'position': 'vocative'}
    assert name_sound_hint('HELLO 士，…', names) == {
        'token': '士', 'target': '匠石', 'matched': '1/2', 'position': 'vocative'}


def test_no_hint_for_unrelated_vocatives_and_latin():
    for text in ('連軍啊', '小左', 'Ross', 'Phoebe'):
        assert name_sound_hint(text, NAMES) is None
    # Pure Latin still yields no hint even when 匠石 is on the names list.
    assert name_sound_hint('Ross', ['匠石']) is None
    assert name_sound_hint('Phoebe', ['匠石']) is None


def test_instruction_contains_meeting_and_weak_hint_rules():
    for phrase in _SINGLE_RULES:
        assert phrase in SINGLE_INSTRUCTION
    for phrase in _ENTRY_RULES:
        assert phrase in ENTRY_INSTRUCTION


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
    for phrase in _SINGLE_RULES:
        assert phrase in model.requests[0].system_extra


def test_meeting_imperative_without_hint_ignore():
    """033/036-style meeting imperative: no name_sound_hint → contract stub yields ignore."""
    stub = RuleContractStub()
    text = '好，翻到下一页，换个方案。'
    decision = VoiceJEV(stub).decide(
        'stone', text, {},
        {'state': 'completed', 'names_for_jiangshi': NAMES,
         'scene': '【当前情境】会议中，助手投影在屏幕上，众人讨论方案。'})
    payload = json.loads(stub.requests[0].input_text)
    assert 'name_sound_hint' not in payload
    assert name_sound_hint(text, NAMES) is None
    assert decision.action == 'ignore'


def test_jiangjiang_weak_hint_yields_to_scene_presence_not_respond():
    """Weak 1/2 hint + scene person 江江 → instruction/payload carry yield; stub must not respond."""
    scene = '【人物与交互】现场有人叫江江，正在吃饭；旁人问他吃了没。'
    stub = RuleContractStub()
    decision = VoiceJEV(stub).decide(
        'stone', '江江，你吃了吗', {},
        {'state': 'completed', 'names_for_jiangshi': NAMES, 'scene': scene})
    payload = json.loads(stub.requests[0].input_text)
    hint = payload['name_sound_hint']
    assert hint['matched'] == '1/2' and hint['token'] == '江江'
    assert '江江' in (payload.get('delivery') or {}).get('scene', '')
    assert weak_hint_yields_to_scene(hint, scene) is True
    assert '仍必须让位为ignore' in stub.requests[0].system_extra
    assert decision.action == 'ignore'


def test_weak_hint_question_tends_respond():
    """将将 + 1/2 + 你觉得呢 → contract stub yields respond (no same-named person in scene)."""
    stub = RuleContractStub()
    decision = VoiceJEV(stub).decide(
        'stone', '将将，你觉得呢？', {},
        {'state': 'completed', 'names_for_jiangshi': NAMES, 'scene': '普通对话，现场只有说话人一人。'})
    payload = json.loads(stub.requests[0].input_text)
    assert payload['name_sound_hint']['matched'] == '1/2'
    assert decision.action == 'respond'


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
    for phrase in _ENTRY_RULES:
        assert phrase in ENTRY_INSTRUCTION
