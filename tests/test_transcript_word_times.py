from jshi.voice.transcripts import TranscriptAssembler
from jshi.voice.cloud_alignment import align_words
from jshi.voice.contracts import SpeechSegment


def test_provider_words_allow_two_speakers_in_one_utterance():
    assembler=TranscriptAssembler()
    rows=assembler.accept({'result':{'utterances':[{
        'text':'还想说两句？别说了，睡了。','start_time':0,'end_time':3000,'definite':True,
        'words':[{'text':'还想说两句？','start_time':0,'end_time':1500},
                 {'text':'别说了，睡了。','start_time':1500,'end_time':3000}],
        'additions':{'speaker_id':'one-provider-track'}}]}})
    assert len(rows)==1 and len(rows[0].words)==2
    parts=align_words(rows[0],(SpeechSegment([], 'a',0,1500),SpeechSegment([], 'b',1500,3000)))
    assert [p.track_id for p in parts]==['a','b']
    assert ''.join(p.text for p in parts)==rows[0].text


def test_invalid_word_times_are_not_invented():
    rows=TranscriptAssembler().accept({'result':{'utterances':[{
        'text':'你好','start_time':0,'end_time':1000,'definite':True,
        'words':[{'text':'你好','start_time':-1,'end_time':1000}]}]}})
    assert rows[0].words==()


def test_punctuation_omitted_from_tokens_preserves_exact_text():
    from jshi.voice.contracts import Transcript, TimedWord
    row=Transcript('你好，睡了。','a',0,2000,True,words=(
        TimedWord('你好',0,900),TimedWord('睡了',1000,2000)))
    parts=align_words(row,(SpeechSegment([], 'a',0,950),SpeechSegment([], 'b',1000,2000)))
    assert len(parts)==2
    assert ''.join(p.text for p in parts)==row.text
    assert [p.track_id for p in parts]==['a','b']
