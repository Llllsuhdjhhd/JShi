from types import SimpleNamespace

import pytest

from jshi.app.voice import VoiceConversation


@pytest.mark.parametrize('source', ['voice', 'mlp'])
def test_reused_identity_discloses_source_without_upgrading_confidence(source):
    host = VoiceConversation.__new__(VoiceConversation)
    host.input_records = {'input': {'personal_classifier':
        {'identity_basis': '沿用'} if source == 'mlp' else {}}}
    host._voice_evidence = lambda *_: {'pick': 'A', 'strength': 'weak',
                                      'basis': '候选', 'score': .45}
    utterance = SimpleNamespace(input_id='input',
        speaker=SimpleNamespace(method='unassigned_audio'),
        identity_note='{"identity_basis":"沿用"}' if source == 'voice' else '{}')
    result = host.voice_evidence(utterance, ())
    assert result == {'pick': 'A', 'strength': 'weak', 'score': .45,
        'basis': '候选；同一连续声音沿用前段人物证据，未重新比对新增尾部'}


def test_manual_confirmation_does_not_inherit_old_automatic_reuse_note():
    host = VoiceConversation.__new__(VoiceConversation)
    host._voice_evidence = lambda *_: {'strength': 'clear', 'basis': '用户人工确认本句归属'}
    utterance = SimpleNamespace(speaker=SimpleNamespace(method='manual_annotation'))
    assert host.voice_evidence(utterance, ())['basis'] == '用户人工确认本句归属'
