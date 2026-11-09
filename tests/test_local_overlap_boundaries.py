from types import SimpleNamespace
from jshi.voice.analysis import SpeakerAnalyzer
from jshi.voice.contracts import Transcript, TimedWord, SpeechSegment
from jshi.voice.cloud_alignment import align_words


def test_brief_overlap_does_not_mark_entire_long_speaker_mixed():
    class Rows(list):
        def sort_by_start_time(self): return self
    class Engine:
        def process(self, audio):
            return Rows([SimpleNamespace(start=0.,end=10.,speaker=0),
                         SimpleNamespace(start=4.,end=5.,speaker=1)])
    segments=SpeakerAnalyzer(Engine(),None).analyze([0]*160000,0)
    clear=[(s.start_ms,s.end_ms) for s in segments if not s.overlap]
    assert clear==[(0,4000),(5000,10000)]
    assert all(4000<=s.start_ms<s.end_ms<=5000 for s in segments if s.overlap)


def test_unknown_words_keep_one_input_without_losing_word_boundaries():
    row=Transcript('甲乙','original',0,2000,True,
        words=(TimedWord('甲',0,900),TimedWord('乙',1100,2000)))
    parts=align_words(row,(SpeechSegment([], 'overlap-a',0,1000,True),
                           SpeechSegment([], 'overlap-b',1000,2000,True)))
    assert [p.text for p in parts]==['甲乙']
    assert parts[0].words == row.words
    assert all(p.identity_uncertain for p in parts)
