from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from jshi.voice.config import VoiceConfig
from jshi.voice.contracts import SpeechSegment, Transcript
from jshi.voice.pipeline import SpeechPipeline
from jshi.voice.semantic_turn import build_semantic_policy, text_view, NamoSemanticPolicy
from jshi.voice.turns import EagerTurnPolicy, TurnCoordinator, TurnDecision, fuse_decisions
from tests.test_voice_turn_strategies import Detector, Analyzer, ASR


@pytest.mark.parametrize('audio,text,expected',[
    ('complete','complete','complete'),('incomplete','incomplete','incomplete'),
    ('complete','incomplete','unknown'),('incomplete','complete','unknown'),
    ('complete','unknown','complete'),('incomplete','unknown','incomplete'),
    ('unknown','complete','complete'),('unknown','incomplete','incomplete'),
    ('unknown','unknown','unknown'),('complete',None,'complete'),
    ('incomplete',None,'incomplete'),('unknown',None,'unknown')])
def test_fusion_agreement_conflict_disabled_and_unavailable(audio,text,expected):
    result=fuse_decisions(TurnDecision(audio,.6),TurnDecision(text,.7) if text else None)
    assert result.state==expected
    if audio==text and text!='unknown':
        assert result.probability is None  # no invented joint confidence


def test_text_starts_before_waiting_for_audio_result():
    text_started=Event();audio_saw_text=Event()
    class Audio(EagerTurnPolicy):
        def analyze_audio(self,samples):
            if text_started.wait(1):audio_saw_text.set()
            return TurnDecision('complete',.8)
    class Text:
        def analyze_text(self,rows):
            text_started.set()
            return TurnDecision('complete',.9)
    p=SpeechPipeline(Detector(),Analyzer(),ASR(),backend='test',
        turns=TurnCoordinator(Audio(),semantic=Text(),timeout_s=1.5))
    try:
        assert len(p.feed(b'a'))==1 and audio_saw_text.is_set()
        assert p.last_timings[0]['turn_reason']=='agree_complete'
        assert p.last_timings[0]['turn_semantic_enabled']
    finally:p.close()


def test_text_reads_pending_and_new_words_without_retranscribing_or_mutating():
    seen=[]
    class Audio(EagerTurnPolicy):
        def analyze_audio(self,samples):
            return TurnDecision('incomplete' if len(samples)==16000 else 'complete',.8)
    class Text:
        def analyze_text(self,rows):
            seen.append(text_view(rows))
            return TurnDecision('incomplete' if len(rows)==1 else 'complete',.8)
    asr=ASR();p=SpeechPipeline(Detector(),Analyzer(),asr,backend='test',
        turns=TurnCoordinator(Audio(),semantic=Text()))
    try:
        assert not p.feed(b'a')
        first=p.last_previews[0]
        rows=p.feed(b'b')
        assert seen==['片段1','片段1\n片段2']
        assert [r.text for r in rows]==['片段1','片段2']
        assert rows[0] is first
        assert asr.calls==[[16000],[16000]]
        assert len({(r.start_ms,r.end_ms) for r in rows})==2
    finally:p.close()


def staged(c, text, start, end, *, cluster='A', voice='', track='same'):
    samples=np.ones((end-start)*16,dtype='float32')
    segment=SpeechSegment(samples,track,start,end,speaker_cluster_id=cluster,voiceprint_id=voice)
    row=segment.transcript(text)
    ticket=c.begin([segment])
    rows,timings=c.finish(ticket,[segment],[row],[{}])
    return rows,timings


def test_people_do_not_mix_even_with_same_asr_track():
    class Audio(EagerTurnPolicy):
        def analyze_audio(self,samples):return TurnDecision('incomplete',.1)
    seen=[]
    class Text:
        def analyze_text(self,rows):
            seen.append(tuple(r.text for r in rows))
            return TurnDecision('incomplete',.1)
    c=TurnCoordinator(Audio(),semantic=Text())
    try:
        assert not staged(c,'甲前半',0,1000,cluster='A')[0]
        assert not staged(c,'乙的话',1000,2000,cluster='B')[0]
        assert not staged(c,'甲后半',2000,3000,cluster='A')[0]
        assert seen==[('甲前半',),('乙的话',),('甲前半','甲后半')]
    finally:c.close()


def test_same_position_revision_replaces_and_released_retry_does_not_repeat():
    class Audio(EagerTurnPolicy):
        state='incomplete'
        def analyze_audio(self,samples):return TurnDecision(self.state,.6)
    audio=Audio();seen=[]
    class Text:
        def analyze_text(self,rows):
            seen.append(text_view(rows));return TurnDecision(audio.state,.6)
    c=TurnCoordinator(audio,semantic=Text())
    try:
        assert not staged(c,'旧转写',0,1000)[0]
        audio.state='complete'
        rows,_=staged(c,'更正转写',0,1000)
        assert [r.text for r in rows]==['更正转写']
        assert seen[-1]=='更正转写'
        assert not staged(c,'更正转写',0,1000)[0]
        rows,_=staged(c,'更正转写',1000,2000)
        assert len(rows)==1  # same words at a new time are real new speech
    finally:c.close()


def test_same_span_identity_revision_is_not_emitted_twice():
    class Audio(EagerTurnPolicy):
        state='incomplete'
        def analyze_audio(self,samples):return TurnDecision(self.state,.6)
    audio=Audio();c=TurnCoordinator(audio)
    try:
        assert not staged(c,'同一段',0,1000,cluster='anonymous')[0]
        audio.state='complete'
        assert len(staged(c,'同一段',0,1000,cluster='anonymous',voice='named')[0])==1
        assert not c.pending
        rest,info=[],[];c.poll(rest,info,final=True)
        assert not rest
    finally:c.close()


@pytest.mark.parametrize('start,end',[(6000,7000),(500,1500)])
def test_long_gaps_or_undeclared_overlap_do_not_glue_old_text(start,end):
    class Audio(EagerTurnPolicy):
        def analyze_audio(self,samples):return TurnDecision('incomplete',.2)
    seen=[]
    class Text:
        def analyze_text(self,rows):
            seen.append(text_view(rows));return TurnDecision('incomplete',.2)
    c=TurnCoordinator(Audio(),semantic=Text())
    try:
        assert not staged(c,'旧半句',0,1000)[0]
        rows,t=staged(c,'新片段',start,end)
        assert [r.text for r in rows]==['旧半句']
        assert t[0]['turn_release_reason']=='continuation_gap_or_overlap'
        assert seen[-1]=='新片段'
    finally:c.close()


def test_semantic_timeout_never_applies_late_result_to_next_turn():
    block=Event();entered=Event()
    class Audio(EagerTurnPolicy):
        n=0
        def analyze_audio(self,samples):
            self.n+=1;return TurnDecision('complete' if self.n==1 else 'incomplete',.7)
    class Text:
        n=0
        def analyze_text(self,rows):
            self.n+=1
            if self.n==1:
                entered.set();block.wait(2);return TurnDecision('complete',.9)
            return TurnDecision('incomplete',.1)
    c=TurnCoordinator(Audio(),semantic=Text(),semantic_timeout_s=.01,max_hold_s=100)
    try:
        assert len(staged(c,'第一轮',0,1000)[0])==1
        assert entered.is_set()
        assert not staged(c,'第二轮前半',1000,2000)[0]
        block.set();c.semantic_future.result(timeout=1)
        assert not staged(c,'第二轮后半',2000,3000)[0]
        assert c.last_events[-1]['reason']=='agree_incomplete'
    finally:block.set();c.close()


def test_old_session_ticket_is_discarded():
    c=TurnCoordinator(EagerTurnPolicy())
    try:
        seg=SpeechSegment(np.ones(16000),'A',0,1000,speaker_cluster_id='A')
        ticket=c.begin([seg]);c.reset()
        assert c.finish(ticket,[seg],[seg.transcript('旧会话')],[{}])==((),[])
        assert not c.pending
    finally:c.close()


def test_semantic_exception_falls_back_to_audio_without_dropping_text():
    class Audio(EagerTurnPolicy):
        def analyze_audio(self,samples):return TurnDecision('complete',.8)
    class Text:
        def analyze_text(self,rows):raise RuntimeError('test model failure')
    c=TurnCoordinator(Audio(),semantic=Text())
    try:
        rows,timings=staged(c,'仍应交给JEV',0,1000)
        assert [r.text for r in rows]==['仍应交给JEV']
        assert timings[0]['turn_reason']=='audio_only'
        assert timings[0]['turn_semantic_reason']=='semantic_error:RuntimeError'
    finally:c.close()


def test_disabled_semantic_has_no_model_dependency(monkeypatch,tmp_path):
    monkeypatch.setenv('JSHI_VOICE_SEMANTIC_TURN','off')
    config=VoiceConfig.from_env(asr='qwen',tts='local')
    assert build_semantic_policy(config,tmp_path) is None
    with pytest.raises(ValueError):replace(config,semantic_turn_strategy='missing')


def test_text_view_preserves_repeated_words_and_does_not_invent_punctuation():
    a=Transcript('不要','A',0,1000,True)
    b=replace(a,start_ms=1000,end_ms=2000)
    assert text_view((b,a))=='不要\n不要'
    assert a.text==b.text=='不要'


def test_namo_uses_official_end_class_and_validates_logits():
    m=NamoSemanticPolicy.__new__(NamoSemanticPolicy)
    m.threshold=.5;m.max_tokens=512
    m.tokenizer=lambda *args,**kwargs:{'input_ids':np.array([[1,2]]),'attention_mask':np.array([[1,1]])}
    logits=[[-2.,2.]]
    m.session=SimpleNamespace(get_inputs=lambda:[SimpleNamespace(name='input_ids'),SimpleNamespace(name='attention_mask')],
        run=lambda outputs,feed:[np.array(logits)])
    assert m.predict('你好').state=='complete'
    logits[:]=[[2.,-2.]]
    assert m.predict('如果').state=='incomplete'
    logits[:]=[[float('nan'),1]]
    with pytest.raises(ValueError):m.predict('测试')
