"""Preparation retains selected/pending people without mutating entry evidence."""
from copy import deepcopy
import json

from jshi.voice.jev_material import preparation_text


def test_background_conversation_cannot_hold_for_completion():
    from tests.test_jev_split_modes import run, entry
    result, model = run('split', entry('wait', 'keep', interaction='none',
                                      utterance_complete='incomplete'))
    assert result.model_error
    assert len(model.requests) == 1
    assert result.route != 'wait'


def test_preparation_scope_preserves_pending_identity_and_source_records():
    snapshot = {
        'lines': [dict(i=i, evidence_ref=f'I{i}', p=f'P{i}', text=f'原话{i}')
                  for i in (1, 2, 3)],
        'scene': 'P1与P2交流，P3在旁边',
        'events': [{'ref': 'E1', 'text': '必要前文', 'at': 12}],
        'short_chat': {'allowed': False}, 'waiting': {'allowed': False},
        'availability': {}, 'environment': {}, 'task_context': [], 'scope': {},
    }
    entry = {
        'items': [dict(i=i, person=f'P{i}', certainty='tentative', why='声音证据')
                  for i in (1, 2, 3)],
        'process_refs': ['I1'], 'pending_refs': ['I2'],
        'decision_refs': ['E1', 'I3'], 'interaction': 'direct',
        'control': 'redirect', 'route': 'prepare',
    }
    originals = deepcopy((snapshot, entry))
    packed = preparation_text(snapshot, entry)
    accepted = json.loads(packed.split('【入口结论】\n', 1)[1].split('\n\n', 1)[0])
    words = json.loads(packed.split('【本次处理原话】\n', 1)[1].split('\n\n', 1)[0])
    assert [row['i'] for row in accepted['items']] == [1, 2]
    assert accepted['pending_refs'] == ['I2']
    assert accepted['decision_refs'] == ['E1', 'I3']
    assert words == [{'i': 1, 'evidence_ref': 'I1', 'text': '原话1'}]
    assert '必要前文' in packed and 'P3在旁边' in packed
    assert (snapshot, entry) == originals
