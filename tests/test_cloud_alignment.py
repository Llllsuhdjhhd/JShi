from dataclasses import replace
from types import SimpleNamespace
import json
import pytest
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


def test_unresolved_words_do_not_split_on_every_competing_segment_boundary():
    original = Transcript('或者继续','p',0,600,True,words=(
        TimedWord('或',0,200),TimedWord('者',200,400),TimedWord('继续',400,600)))
    parts=align_words(original,[segment(0,300,'a',True),segment(300,600,'b',True)])
    assert len(parts)==1
    assert parts[0].text=='或者继续'
    assert parts[0].identity_uncertain


@pytest.mark.parametrize('text,tokens', [
    ('匠石。', ('匠', '石')), ('小王，帮我看看。', ('小', '王，帮我看看')),
    ('Hello there!', ('Hello', 'there')),
])
def test_uncertain_prefix_keeps_provider_group_without_claiming_tail_identity(text,tokens):
    source=Transcript(text,'1',0,1400,True,words=(
        TimedWord(tokens[0],100,200),TimedWord(tokens[1],500,1300)))
    parts=align_words(source,[segment(0,400,'mixed',True),segment(400,1400,'qf')])
    assert len(parts)==1 and parts[0].text==text and parts[0].words==source.words
    assert (parts[0].start_ms,parts[0].end_ms,parts[0].track_id)==(0,1400,'1')
    assert parts[0].identity_uncertain and parts[0].overlap
    assert not parts[0].voiceprint_id and not parts[0].speaker_cluster_id
    assert json.loads(parts[0].identity_note)['grouping_basis']=='provider_single_local_uncertain_prefix'


def test_recorded_short_vocative_boundaries_use_provider_group():
    source=Transcript('匠石。','1',573711,574351,True,words=(
        TimedWord('匠',573991,574031),TimedWord('石',574311,574351)))
    parts=align_words(source,[segment(573809,574281,'mixed',True),segment(574281,574351,'qf')])
    assert len(parts)==1 and parts[0].text=='匠石。'
    assert parts[0].identity_uncertain and not parts[0].voiceprint_id


@pytest.mark.parametrize('track,provider_overlap', [('unidentified-0',False),('',False),('1',True)])
def test_no_provider_single_voice_evidence_keeps_ambiguous_split(track,provider_overlap):
    source=Transcript('小王',track,0,1000,True,overlap=provider_overlap,
                      words=(TimedWord('小',100,200),TimedWord('王',500,900)))
    assert len(align_words(source,[segment(0,300,'a',True),segment(300,1000,'b')]))==2


def test_uncertain_prefix_does_not_hide_real_two_voice_evidence():
    source=Transcript('喂你先走我留下','1',0,1800,True,words=(
        TimedWord('喂',0,200),TimedWord('你先走',400,800),TimedWord('我留下',1000,1600)))
    parts=align_words(source,[segment(0,300,'x',True),segment(300,900,'a'),segment(900,1800,'b')])
    assert [p.text for p in parts]==['喂','你先走','我留下']


def test_middle_overlap_is_annotated_without_splitting_single_voice_sentence():
    source=Transcript('你先走吧','1',0,1400,True,words=(
        TimedWord('你先',0,400),TimedWord('走',500,700),TimedWord('吧',900,1300)))
    parts=align_words(source,[segment(0,450,'a'),segment(450,800,'mixed',True),segment(800,1400,'a')])
    assert len(parts)==1 and parts[0].text==source.text
    assert parts[0].overlap and parts[0].identity_uncertain
    assert json.loads(parts[0].identity_note)['local_overlap_ranges']==[[450,800]]


def test_recorded_204643_overlap_keeps_correct_sentence():
    # Recorded acoustic/word grouping boundaries; grouped tokens stand for words.
    source=Transcript('也不知道是谁，这么欠打。','9',456812,459812,True,words=(
        TimedWord('也不知',456852,457132),TimedWord('道是谁',457132,457772),
        TimedWord('这么欠打',458212,459292)))
    parts=align_words(source,[segment(456890,457144,'a'),
        segment(457144,457616,'mixed',True),segment(457616,459287,'a')])
    assert len(parts)==1 and parts[0].text==source.text
    assert parts[0].identity_uncertain and parts[0].overlap


def test_one_short_segment_reports_coverage_not_multiple_speakers():
    source = Transcript('hello，将士。', '1', 39652, 41542, True)
    result, = align_words(source, [segment(39882, 41309, 'lux')])
    reason = json.loads(result.identity_note)['reason']
    assert '75.5%' in reason and '未达80%' in reason
    assert '多个声音' not in reason
    assert result.identity_uncertain and not result.voiceprint_id


@pytest.mark.parametrize('start,end,overlap,extra,available,accepted', [
    (39882,41309,False,False,True,True),
    (39953,41309,False,False,True,False),
    (39882,41241,False,False,True,False),
    (39882,41309,True,False,True,False),
    (39882,41309,False,True,True,False),
    (39882,41309,False,False,False,False),
])
def test_single_voice_short_edges_use_real_audio_before_identification(start,end,overlap,extra,available,accepted):
    import numpy as np
    source = Transcript('hello，将士。', '1', 39652, 41542, True)
    pcm = (np.arange(30240, dtype=np.int16)).tobytes()
    spans = [replace(segment(start,end,'local',overlap), voiceprint_id='', confidence=None)]
    if extra:
        spans.append(segment(39652,39800,'other'))
    seen = []
    def identify(rows):
        seen.extend(rows)
        return tuple(replace(r,voiceprint_id='lux-voice',confidence=.441) if len(r.samples)>=24000 else r for r in rows)
    aligner = CloudSpeakerAlignment(SimpleNamespace(segment=lambda *a: pcm if available else None),
        SimpleNamespace(identify_segments=identify), segments=SimpleNamespace(for_range=lambda *a:spans))
    result, = aligner.process(source)
    if accepted:
        assert result.voiceprint_id == 'lux-voice' and not result.identity_uncertain
        assert (seen[0].start_ms,seen[0].end_ms)==(39652,41542)
        np.testing.assert_array_equal(seen[0].samples,np.frombuffer(pcm,dtype='<i2').astype('float32')/32768)
    else:
        assert not result.voiceprint_id and result.identity_uncertain
