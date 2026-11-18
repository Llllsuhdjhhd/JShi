"""Regression materials from the external-AI conversation, without a live model."""
from copy import deepcopy
import pytest

from jshi.voice.jev_material import build_snapshot, entry_text, recent_context_for_main
from jshi.voice.jev_scene import JEVSceneStore, recent_material
from jshi.voice.scene_material import dialogue_row


def external_exchange():
    # Recorded phrases; times below are synthetic ordering offsets.
    return [
        {'ref': 'E1', 'p': 'P2', 'text': '怎么下模组？', 'at': -17},
        {'ref': 'E2', 'p': '匠石', 'text': '你用的是哪个软件？', 'at': -10},
        {'ref': 'E3', 'p': 'unknown', 'text': '先确认26.3带fabric加载器', 'at': -3},
        {'ref': 'E4', 'p': 'P1', 'text': '降石没有跟你说话。', 'at': -1},
    ]


def test_entry_material_preserves_external_chain_without_self_proof():
    events = external_exchange()
    original = deepcopy(events)
    payload = {'input': {'lines': [], 'unwritten': events},
               'scene': 'luguang跟着匠石装光影', 'conversation': []}
    snapshot = build_snapshot(payload, [], {})
    assert events == original
    assert [r['ref'] for r in snapshot['events']] == ['E1', 'E2', 'E3', 'E4']
    assert [r['evidence_role'] for r in snapshot['events']] == [
        'external_input', 'self_output', 'external_input', 'external_input']
    assert snapshot['events'][2]['p'] == 'unknown'  # No invented AI identity.
    text = entry_text(snapshot)
    assert 'fabric' in text and '没有跟你说话' in text
    assert 'self_output' in text and 'external_input' in text
    main = recent_context_for_main(snapshot)
    assert '自身输出，仅记录已说，不证明交往成立' in main
    assert '外部原话，受话对象待结合问答判断' in main
    assert 'fabric' in main and '没有跟你说话' in main


def test_scene_writer_keeps_provenance_references_and_original_records(tmp_path):
    store = JEVSceneStore(tmp_path / 'scene.json')
    for n, event in enumerate(external_exchange()):
        store.append(f'raw-{n}', event)
    before = store.snapshot()
    snapshot = deepcopy(before)
    rows, _ = recent_material(snapshot, '')
    assert [r['evidence_role'] for r in rows] == [
        'external_input', 'self_output', 'external_input', 'external_input']
    assert len({r['ref'] for r in rows}) == 4
    assert store.snapshot() == before
    assert all('evidence_role' not in r['payload'] for r in before['events'])


def test_interpretations_are_not_external_observations_or_keyword_classification():
    assert dialogue_row({'kind': 'understanding_update', 'text': '双方正在交谈'})[
        'evidence_role'] == 'interpretation'
    assert dialogue_row({'p': 'unknown', 'text': '我是匠石，我来指导你'})[
        'evidence_role'] == 'external_input'
    assert 'evidence_role' not in dialogue_row({'kind': 'visual_environment', 'text': '有人看向设备'})


@pytest.mark.parametrize('field', ['p', 'who', 'speaker'])
def test_actual_dialogue_speaker_formats_do_not_turn_self_output_into_external_evidence(field):
    from jshi.voice.scene_material import mark_evidence_role
    row = {field:'匠石', 'text':'这是兄友弟恭吧。', 'basis':'已播', 'at':-8}
    assert mark_evidence_role(row)['evidence_role'] == 'self_output'
    payload = {'input':{'lines':[], 'unwritten':[]}, 'conversation':[row]}
    snapshot = build_snapshot(payload, [], {})
    assert snapshot['events'][0]['evidence_role'] == 'self_output'
    assert '自身输出，仅记录已说' in recent_context_for_main(snapshot)
    assert '外部原话' not in recent_context_for_main(snapshot)


def test_actual_external_correction_remains_external_beside_self_explanation():
    payload = {'input':{'lines':[]}, 'conversation':[
        {'who':'匠石','text':'你是想让我讲胸这个字？','at':-12,'basis':'已播'},
        {'who':'未命名访客 799','text':'哎，匠石没有跟你说。','at':-10,'basis':'收到的原话'},
        {'who':'qf','text':'兄友弟，恭。','at':-1,'basis':'收到的原话'}]}
    original = deepcopy(payload)
    snapshot = build_snapshot(payload, [], {})
    assert [r['evidence_role'] for r in snapshot['events']] == [
        'self_output', 'external_input', 'external_input']
    assert payload == original  # Retain ASR punctuation/ambiguity, never rewrite testimony.
