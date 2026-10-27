from types import SimpleNamespace
from threading import Event
from dataclasses import replace

import numpy as np
import pytest

from jshi.voice.config import VoiceConfig
from jshi.voice.contracts import SpeechSegment, Transcript
from jshi.voice.pipeline import SpeechPipeline
from jshi.voice.segmentation import SileroSegmenter, segment_settings
from jshi.voice.turns import EagerTurnPolicy, TurnCoordinator, TurnDecision
from tests.test_voice import process, FakeCloud


class Detector:
    def __init__(self): self.offset=0
    def reset(self): self.offset=0
    def feed(self, pcm, final=False):
        if not pcm: return ()
        offset=self.offset; self.offset+=1000
        return ((np.ones(16000), offset),)


class Analyzer:
    speakers=None
    def analyze(self, samples, start):
        return (SpeechSegment(samples,'A',start,start+1000,speaker_cluster_id='cluster-A'),)


class ASR:
    def __init__(self): self.calls=[]
    def transcribe_many(self, audios):
        self.calls.append([len(a) for a in audios])
        return ['片段'+str(len(self.calls)) for _ in audios]


def test_new_defaults_and_independent_segment_strategies(monkeypatch):
    for key in ('JSHI_VOICE_END_WINDOW_MS','JSHI_VOICE_MAX_WINDOW_MS','JSHI_VOICE_SEGMENT_STRATEGY'):
        monkeypatch.delenv(key,raising=False)
    config=VoiceConfig.from_env(asr='qwen',tts='local')
    fixed=segment_settings(config)
    assert fixed.silence_ms == 500 and fixed.max_window_s == 3.5
    assert config.turn_strategy == 'smart_turn' and config.turn_threshold == .5
    patient=segment_settings(replace(config,segment_strategy='patient'))
    assert patient.silence_ms == 1000 and patient.max_window_s == 3.5
    with pytest.raises(ValueError): replace(config,turn_strategy='missing')
    with pytest.raises(ValueError): replace(config,max_window_s=0)


def test_audio_prediction_and_single_asr_batch_actually_overlap():
    audio_started, asr_started = Event(),Event()
    class Policy(EagerTurnPolicy):
        def analyze_audio(self, samples):
            audio_started.set()
            assert asr_started.wait(1), 'ASR must run alongside audio prediction'
            return TurnDecision('complete',.8)
    class ParallelASR(ASR):
        def transcribe_many(self, audios):
            asr_started.set()
            assert audio_started.wait(1)
            return super().transcribe_many(audios)
    asr=ParallelASR(); turns=TurnCoordinator(Policy())
    pipeline=SpeechPipeline(Detector(),Analyzer(),asr,backend='test',turns=turns)
    try:
        assert len(pipeline.feed(b'a')) == 1
        assert asr.calls == [[16000]]
        assert pipeline.last_timings[0]['turn_state'] == 'complete'
    finally: pipeline.close()


def test_incomplete_appends_context_but_never_retranscribes_old_audio():
    lengths=[]
    class Policy(EagerTurnPolicy):
        def analyze_audio(self, samples):
            lengths.append(len(samples))
            return TurnDecision('incomplete' if len(lengths)==1 else 'complete',.2 if len(lengths)==1 else .8)
    asr=ASR(); pipeline=SpeechPipeline(Detector(),Analyzer(),asr,backend='test',turns=TurnCoordinator(Policy()))
    try:
        assert pipeline.feed(b'a') == ()
        assert pipeline.last_previews[0].text == '片段1'
        rows=pipeline.feed(b'b')
        assert [r.text for r in rows] == ['片段1','片段2']
        assert [(r.start_ms,r.end_ms) for r in rows] == [(0,1000),(1000,2000)]
        assert asr.calls == [[16000],[16000]]
        assert lengths == [16000,32000]
        assert not pipeline.turns.pending
    finally: pipeline.close()


def test_hold_timeout_and_capture_end_release_instead_of_dropping(monkeypatch):
    import jshi.voice.turns as mod
    now=[100.]
    monkeypatch.setattr(mod,'monotonic',lambda:now[0])
    class Policy(EagerTurnPolicy):
        def analyze_audio(self,samples): return TurnDecision('incomplete',.1)
    p=SpeechPipeline(Detector(),Analyzer(),ASR(),backend='test',turns=TurnCoordinator(Policy(),max_hold_s=2))
    try:
        assert p.feed(b'a') == ()
        now[0]+=2.1
        rows=p.feed(b'')
        assert len(rows)==1 and p.last_timings[0]['turn_release_reason']=='hold_limit'
        assert p.feed(b'b') == ()
        assert len(p.feed(b'',final=True)) == 1
        assert p.last_timings[0]['turn_release_reason']=='capture_end'
    finally:p.close()


def test_overlap_and_unresolved_tracks_are_not_glued_to_people():
    assert TurnCoordinator.key(Transcript('a','0',0,100,True)) is None
    assert TurnCoordinator.key(Transcript('a','0',0,100,True,overlap=True,voiceprint_id='A')) is None
    assert TurnCoordinator.key(Transcript('a','0',0,100,True,voiceprint_id='A')) != TurnCoordinator.key(Transcript('b','0',100,200,True,voiceprint_id='B'))


def test_busy_audio_classifier_has_bounded_fallback():
    block=Event()
    class Policy(EagerTurnPolicy):
        def analyze_audio(self,samples):
            block.wait(2)
            return TurnDecision('incomplete',.1)
    p=SpeechPipeline(Detector(),Analyzer(),ASR(),backend='test',turns=TurnCoordinator(Policy(),timeout_s=.01))
    try:
        assert len(p.feed(b'a')) == 1
        assert len(p.feed(b'b')) == 1
        assert not p.turns.pending
        assert p.last_timings[0]['turn_state']=='unknown'
    finally:block.set();p.close()


def test_stage_one_enforces_max_audio_size_without_losing_samples():
    class VAD:
        def __init__(self):self.queue=[]
        def accept_waveform(self,a):pass
        def empty(self):return not self.queue
        @property
        def front(self):return self.queue[0]
        def pop(self):self.queue.pop(0)
        def flush(self):self.queue.append(SimpleNamespace(start=0,samples=np.arange(120000,dtype='float32')))
    detector=SileroSegmenter(VAD(),max_window_s=3.5)
    rows=detector.feed(b'\0\0'*120000,final=True)
    assert all(len(audio)<=56000 for audio,start in rows)
    assert sum(len(audio) for audio,start in rows)==120000
    assert [start for audio,start in rows]==[0,3500,7000]


def test_jev_hold_survives_scene_summarization_and_does_not_repeat_current(process,monkeypatch):
    import asyncio
    from time import time
    from jshi.app.voice import VoiceConversation
    from jshi.core.envelope import SceneUtterance, SpeakerEvidence
    async def run():
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            now=time()*1000
            a=SceneUtterance('如果明天下雨',SpeakerEvidence('A',object_id='person-a',label='甲'),
                0,1000,input_id='old',received_at_ms=now-100)
            b=replace(a,text='我们还去吗',input_id='new',received_at_ms=now)
            c._hold_jev_inputs((a,))
            monkeypatch.setattr(c.jev_scene_store,'view',lambda *args:('已有摘要',[]))
            context,_=c._jev_context((b,))
            assert len(context)==1 and context[0]['text']=='如果明天下雨'
            assert context[0]['entry_status']=='pending_continuation'
            assert context[0]['p']!='unknown'
            assert c._jev_context((a,))[0] == []
            c._consume_jev_inputs((replace(b,speaker=SpeakerEvidence('B',object_id='person-b',label='乙')),))
            assert c.jev_pending_inputs
            c._consume_jev_inputs((b,))
            assert not c.jev_pending_inputs
        finally:await c.close()
    asyncio.run(run())


def test_releasing_large_deferred_batch_uses_backpressure_without_dropping(process):
    import asyncio
    from jshi.app.voice import VoiceConversation
    from jshi.voice.jev import VoiceJEV
    from tests.test_voice_window import Gate
    async def run():
        async def send(m):await asyncio.sleep(0)
        gate=Gate();c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(gate, mode='legacy'))
        # Workers await self.queue dynamically; keep the queue object but reduce capacity.
        c.queue._maxsize=2
        try:
            rows=[Transcript('片段'+str(i),'A',i*100,(i+1)*100,True) for i in range(6)]
            await asyncio.wait_for(c.accept_batch(rows),3)
            await asyncio.wait_for(c.queue.join(),3)
            assert len(c.input_records)==6
            assert sum(len(batch) for batch in gate.batches)==6
        finally:await c.close()
    asyncio.run(run())
