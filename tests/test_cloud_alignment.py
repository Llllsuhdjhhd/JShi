from dataclasses import replace
from types import SimpleNamespace
from jshi.voice.contracts import Transcript, TimedWord, SpeechSegment
from jshi.voice.cloud_alignment import align_words, CloudSpeakerAlignment


def segment(start, end, who, overlap=False):
    return SpeechSegment([], who, start, end, overlap, 'voice-' + who, .8)


def row():
    return Transcript('讲完这题。为什么？', 'unidentified-0', 0, 2000, True,
        words=(TimedWord('讲完', 0, 500), TimedWord('这题。', 500, 1000), TimedWord('为什么？', 1200, 2000)))


def test_switch_speaker_splits_transcript_without_losing_words():
    original = row()
    parts = align_words(original, [segment(0, 1000, 'qf'), segment(1200, 2000, 'child')])
    assert [p.text for p in parts] == ['讲完这题。', '为什么？']
    assert [p.track_id for p in parts] == ['qf', 'child']
    assert ''.join(p.text for p in parts) == original.text
    assert [(p.start_ms, p.end_ms) for p in parts] == [(0, 1000), (1200, 2000)]
    assert original.track_id == 'unidentified-0'


def test_overlap_and_missing_words_cannot_be_one_named_speaker():
    spans = [segment(0, 1600, 'qf', True), segment(1000, 2000, 'child', True)]
    parts = align_words(row(), spans)
    assert all(p.identity_uncertain and not p.voiceprint_id for p in parts)
    parts = align_words(replace(row(), words=()), spans)
    assert len(parts) == 1 and parts[0].identity_uncertain and not parts[0].voiceprint_id
    parts = align_words(replace(row(), words=(TimedWord('错误内容', 0, 1000),)), spans)
    assert parts[0].identity_uncertain and parts[0].text == row().text


def test_revision_uses_only_ranges_actually_delivered():
    original = row()
    pipeline = CloudSpeakerAlignment(None, None)
    pipeline.remember(original, (original,))  # timeout fallback was delivered
    corrected = replace(original, text='更正', replaces=((0, 2000),))
    assert pipeline.process(corrected)[0].replaces == ((0, 2000),)
    parts = align_words(original, [segment(0, 1000, 'qf'), segment(1200, 2000, 'child')])
    pipeline.remember(original, parts)
    assert pipeline.process(corrected)[0].replaces == ((0, 1000), (1200, 2000))


def test_unaligned_span_does_not_gain_scene_identity_candidates():
    from jshi.voice.jev import VoiceJEV
    from tests.test_jev_split_modes import Model, entry
    answer = entry(route='background', interaction='none', control='keep', process_refs=[])
    answer['items'][0].update(person='unknown', certainty='unknown')
    model = Model([answer])
    value = VoiceJEV(model, mode='split', fallback_models=[]).decide_batch('s', [
        {'n':1,'who':'unknown','text':'两人的话尚未切开','person_scope':'unaligned_cloud_span','candidates':[]}], [],
        {'people':{'P1':'qf'},'jev_scene':'P1在讲题。'})
    assert value.judged and value.items[0].speaker_pick == 'unknown'
    assert '"person_scope":"unaligned_cloud_span"' in model.requests[0].input_text
    assert '"source":"scene"' not in model.requests[0].input_text
