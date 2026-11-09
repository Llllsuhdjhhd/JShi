import asyncio
from threading import Event
from types import SimpleNamespace

from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.audio_timeline import AudioTimeline


def test_analysis_runs_without_any_cloud_transcript_and_reports_overlap():
    async def run():
        audio = SessionAudio()
        rows = []
        completed = asyncio.Event()
        def report(kind, value):
            rows.append((kind, value))
            completed.set()
        def analyze(samples, start):
            assert len(samples) == 32000
            return [SimpleNamespace(start_ms=start, end_ms=start+2000,
                track_id='window-A', voiceprint_id='', overlap=True)]
        timeline = AudioTimeline(audio, SimpleNamespace(analyze=analyze), report)
        try:
            audio.append(b'\0\0'*32000)
            timeline.feed()
            await asyncio.wait_for(completed.wait(), 2)
            assert rows[0][0] == 'audio_timeline_window'
            assert rows[0][1]['parts'][0]['overlap']
            assert rows[0][1]['shadow']
        finally:
            await timeline.close()
    asyncio.run(run())


def test_slow_analyzer_coalesces_packets_and_reports_uncovered_audio():
    async def run():
        audio = SessionAudio(seconds=60)
        entered, release = Event(), Event()
        rows = []
        completed = asyncio.Event()
        calls = []
        def analyze(samples, start):
            calls.append(start)
            if len(calls) == 1:
                entered.set()
                assert release.wait(3)
            return ()
        def report(kind, value):
            rows.append((kind, value))
            if kind == 'audio_timeline_window' and value['end_ms'] == 22000:
                completed.set()
        timeline = AudioTimeline(audio, SimpleNamespace(analyze=analyze), report)
        try:
            audio.append(b'\0\0'*32000)
            timeline.feed()
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(200):
                audio.append(b'\0\0'*1600)
                timeline.feed()
            release.set()
            await asyncio.wait_for(completed.wait(), 3)
            assert len(calls) == 2
            gap = next(v for k,v in rows if k == 'audio_timeline_gap')
            assert (gap['start_ms'], gap['end_ms']) == (2000, 14000)
        finally:
            release.set()
            await timeline.close()
    asyncio.run(run())


def test_long_speaker_keeps_origin_until_the_eventual_boundary():
    import numpy as np
    from jshi.voice.contracts import SpeechSegment
    from jshi.voice.segmentation_cache import SegmentationCache
    async def run():
        audio=SessionAudio()
        def analyze(samples,start):
            end=min(20000,start+len(samples)//16)
            return (SpeechSegment(samples[:max(0,end-start)*16],'a',start,end),) if end>start else ()
        cache=SegmentationCache(audio,SimpleNamespace(analyze=analyze))
        complete=asyncio.Event();observed=[]
        async def offer(parts,start,end):
            observed.extend(parts);complete.set()
        timeline=AudioTimeline(audio,cache,lambda *a:None,on_window=offer)
        try:
            for end in range(1000,22000,1000):
                audio.append(b'\0\0'*16000);complete.clear();timeline.feed()
                await asyncio.wait_for(complete.wait(),2)
            assert observed[-1].start_ms==0 and observed[-1].end_ms==20000
        finally:await timeline.close()
    asyncio.run(run())
