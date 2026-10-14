import copy
import json

from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV
from jshi.voice.jev_input import compact_jev_input


def test_compaction_preserves_evidence_and_reference_relationships():
    now = 1791700000123.45
    frame = 'image-' + 'a' * 32
    old_frame = 'image-' + 'b' * 32
    track = 'session-' + 'c' * 32 + ':speaker-1'
    payload = {'now': now, 'input': {
        'lines': [{'i': 1, 'p': 'P1', 'text': '你看得到我吗', 'at': now - 150.4, 'track': track}],
        'unwritten': [{'input_id': 'event-' + 'd' * 32, 'at': now - 2000, 'track': track,
                       'judgment': {'person': 'P1', 'certainty': 'confirmed', 'why': '声纹明确'}}],
        'envelope': {'input_id': 'event-' + 'e' * 32,
                     'speaker': {'object_id': 'OBJ-' + 'f' * 32, 'track_id': track,
                                 'label': 'lux', 'method': 'user_selected'},
                     'parts': [{'kind': 'text', 'text': '你看得到我吗'},
                               {'kind': 'image', 'reference': frame, 'image_url': 'data:image/jpeg;base64,photo'}],
                     'start_ms': 1200.7, 'end_ms': 2200.8,
                     'environment': {'id': 'snapshot-' + 'a' * 32, 'frame': frame,
                                     'description_frame': old_frame, 'captured': (now - 400) / 1000,
                                     'described_at': (now - 12000) / 1000, 'completed': 0,
                                     'envelope': {'clock_offset': 120.000001, 'source_time': 1234.56789},
                                     'description': 'lux所述的室内环境'}}},
        'scene': 'P1与匠石交流', 'short_chat': {'allowed': True, 'recent': [
            {'subject': 'stone', 'object_id': 'OBJ-' + 'f' * 32, 'input': '你好', 'reply': '你好'}]}}
    original = copy.deepcopy(payload)
    compact = compact_jev_input(payload)
    assert payload == original
    line = compact['input']['lines'][0]
    envelope = compact['input']['envelope']
    assert line['p'] == 'P1' and line['i'] == 1
    assert line['track'] == compact['input']['unwritten'][0]['track'] == envelope['speaker']['track_id'] == 'T1'
    assert line['at'] == -150
    assert compact['input']['unwritten'][0]['at'] == -2000
    assert envelope['environment']['captured'] == -400
    assert envelope['environment']['described_at'] == -12000
    assert envelope['environment']['completed'] is None
    assert envelope['start_ms'] == 1201 and envelope['end_ms'] == 2201
    assert envelope['parts'][0]['reference'] == envelope['environment']['frame'] == 'M1'
    assert envelope['environment']['description_frame'] == 'M2'
    assert 'envelope' not in envelope['environment']
    assert all(p['kind'] != 'text' for p in envelope['parts'])
    assert compact['input']['unwritten'][0]['judgment'] == original['input']['unwritten'][0]['judgment']
    assert compact['short_chat']['recent'] == [{'input': '你好', 'reply': '你好'}]
    serialized = json.dumps(compact, ensure_ascii=False)
    assert frame not in serialized and track not in serialized and 'OBJ-' not in serialized
    assert 'image_url' not in serialized
    assert len(serialized) < len(json.dumps(original, ensure_ascii=False))


def test_compact_text_keeps_original_multimodal_transport_and_output_identity():
    class Model:
        def generate(self, request):
            self.request = request
            return ModelResponse(model='fake', text=json.dumps({
                'items': [{'i': 7, 'person': 'P1', 'certainty': 'confirmed', 'why': '用户所选人物'}],
                'level': 3, 'recall_memory': False, 'route': 'main', 'reply': ''}))

    model = Model()
    packed = {'input_id': 'a' * 32, 'source': 'web_text',
              'speaker': {'object_id': 'OBJ-' + 'b' * 32, 'method': 'user_selected', 'label': 'lux'},
              'parts': [{'kind': 'image', 'reference': 'c' * 32, 'image_url': 'data:image/jpeg;base64,photo'}]}
    original = copy.deepcopy(packed)
    result = VoiceJEV(model).decide_batch('stone', [
        {'n': 7, 'who': 'P1', 'source': 'web_text', 'text': '你好', 'at': 1000.25}], [],
        {'now': 1100.75, 'envelope': packed})
    data = json.loads(model.request.input_text)
    assert data['input']['lines'][0]['at'] == -101
    assert data['input']['envelope']['parts'][0]['reference'] == 'M1'
    assert model.request.input_parts[0] == packed['parts'][0]
    assert packed == original
    assert result.judged and result.items[0].n == 7 and result.items[0].speaker_pick == 'P1'


def test_missing_clock_does_not_invent_relative_time():
    compact = compact_jev_input({'input': {'lines': [{'at': 1234.7}]}, 'now': None})
    assert compact['input']['lines'][0]['at'] == 1235
    assert 'time_basis' not in compact['input']
