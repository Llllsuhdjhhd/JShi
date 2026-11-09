import numpy as np
import pytest

from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.contracts import SpeechSegment, Transcript, TimedWord
from jshi.voice.segmentation_cache import SegmentationCache
from jshi.voice.analysis import SpeakerAnalyzer
from jshi.voice.cloud_alignment import CloudSpeakerAlignment


class Acoustic:
    def __init__(self):
        self.calls = []

    def analyze(self, samples, start):
        end = start + len(samples)//16
        self.calls.append((start, end))
        # A real speaker boundary, unchanged across overlapping windows.
        return tuple(SpeechSegment(samples[(a-start)*16:(b-start)*16], name, a, b)
                     for a, b, name in [(start, min(end, 9000), 'a'), (max(start, 9000), end, 'b')]
                     if b > a)


def test_18_seconds_are_preprocessed_and_final_reuses_all_windows():
    audio, engine = SessionAudio(), Acoustic()
    cache = SegmentationCache(audio, engine)
    for end in (6000, 12000, 18000):
        audio.append(b'\0\0' * 96000)
        start = max(0, end-8000)
        cache.analyze(np.zeros((end-start)*16), start)
    before = list(engine.calls)
    parts = cache.for_range(0, 18000)
    assert engine.calls == before
    assert [(s.start_ms, s.end_ms) for s in parts] == [(0,9000),(9000,18000)]
    assert parts[0].track_id != parts[1].track_id
    assert all(len(s.samples) == (s.end_ms-s.start_ms)*16 for s in parts)


def test_backlog_gaps_are_chunked_and_tail_rounding_does_not_create_audio():
    audio, engine = SessionAudio(), Acoustic()
    audio.append(b'\0\0'*288000)
    cache = SegmentationCache(audio, engine)
    cache.for_range(0, 18020)
    assert len(engine.calls) == 3 and all(b-a <= 8000 for a,b in engine.calls)
    assert max(p.end_ms for p in cache.for_range(0,18020)) == 18000
    with pytest.raises(ValueError):
        cache.for_range(0,18100)


def test_short_final_uses_rolling_context_without_second_segmentation():
    audio, engine = SessionAudio(), Acoustic()
    audio.append(b'\0\0'*128000)
    cache = SegmentationCache(audio, engine)
    cache.analyze(np.zeros(128000), 0)
    alignment = CloudSpeakerAlignment(audio, SpeakerAnalyzer(None,None), segments=cache)
    source = Transcript('匠石。','provider',1000,1640,True,
                        words=(TimedWord('匠',1100,1140),TimedWord('石',1420,1460)))
    rows = alignment.process(source)
    assert [r.text for r in rows] == ['匠石。']
    assert len(engine.calls) == 1
    assert rows[0].words == source.words


def test_one_provider_speaker_two_acoustic_speakers_use_words_without_new_asr():
    audio, engine = SessionAudio(), Acoustic()
    audio.append(b'\0\0'*192000)
    cache = SegmentationCache(audio, engine)
    cache.analyze(np.zeros(128000),4000)
    alignment = CloudSpeakerAlignment(audio, SpeakerAnalyzer(None,None), segments=cache)
    source = Transcript('甲说完乙继续','provider',8000,10000,True,
        words=(TimedWord('甲说完',8000,8900),TimedWord('乙继续',9100,10000)))
    rows = alignment.process(source)
    assert [r.text for r in rows] == ['甲说完','乙继续']
    assert rows[0].speaker_cluster_id != rows[1].speaker_cluster_id
    assert len(engine.calls) == 1


def test_overlapping_windows_do_not_insert_the_next_windows_leading_gap():
    class LeadingEdge:
        def analyze(self,samples,start):
            return (SpeechSegment(samples[480:],'a',start+30,start+len(samples)//16),)
    audio=SessionAudio();audio.append(b'\0\0'*144000)
    cache=SegmentationCache(audio,LeadingEdge())
    cache.analyze(np.zeros(128000),0)
    cache.analyze(np.zeros(128000),1000)
    parts=cache.for_range(900,1200)
    assert [(s.start_ms,s.end_ms) for s in parts]==[(900,1200)]
    # The same edge must remain reliable when reading the entire accumulated
    # sentence, not only a tiny interval around the artificial window start.
    parts=cache.for_range(0,9000)
    assert [(s.start_ms,s.end_ms) for s in parts]==[(30,9000)]


def test_demand_tail_resets_cadence_but_cache_reads_and_old_repairs_do_not():
    import asyncio
    from jshi.voice.audio_timeline import AudioTimeline
    async def run():
        audio,engine=SessionAudio(),Acoustic()
        audio.append(b'\0\0'*144000)
        cache=SegmentationCache(audio,engine)
        cache.analyze(np.zeros(128000),1000)
        timeline=AudioTimeline(audio,cache,lambda *a:None)
        try:
            audio.append(b'\0\0'*9600)
            cache.for_range(8000,9600)
            assert engine.calls[-1] == (7000,9600)
            timeline.feed()
            assert not timeline.wake.is_set()
            cache.for_range(0,500)  # older repair cannot reset the newest clock
            assert cache.computed_end_ms == 9600
            before=list(engine.calls)
            audio.append(b'\0\0'*14400)  # 10.5 s, only .9 s after demand work
            cache.for_range(8000,9000)  # cache read must not reset cadence
            timeline.feed()
            assert not timeline.wake.is_set() and engine.calls == before
            audio.append(b'\0\0'*1600)
            timeline.feed()
            assert timeline.wake.is_set()
        finally: await timeline.close()
    asyncio.run(run())


def test_final_before_first_rolling_tick_computes_immediately():
    import asyncio
    from jshi.voice.audio_timeline import AudioTimeline
    async def run():
        audio,engine=SessionAudio(),Acoustic()
        cache=SegmentationCache(audio,engine)
        timeline=AudioTimeline(audio,cache,lambda *a:None,step_ms=1000)
        try:
            audio.append(b'\0\0'*8000)  # only 0.5 seconds; 06 has not fired
            timeline.feed()
            assert not timeline.wake.is_set() and not engine.calls
            alignment=CloudSpeakerAlignment(audio,SpeakerAnalyzer(None,None),segments=cache)
            row=Transcript('你好','p',0,500,True,words=(TimedWord('你好',50,450),))
            result=await asyncio.wait_for(asyncio.to_thread(alignment.process,row),.5)
            assert result[0].text=='你好'
            assert engine.calls==[(0,500)] and timeline.covered_ms==0
            assert not timeline.wake.is_set()
        finally:await timeline.close()
    asyncio.run(run())
