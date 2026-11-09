from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, RLock
from types import SimpleNamespace

import numpy as np
import pytest

from jshi.voice.analysis import ParallelIdentity
from jshi.voice.contracts import SpeechSegment
from jshi.voice.speakers import LocalSpeakers
from jshi.voice.personal import PersonalSpeakers
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.segmentation_cache import SegmentationCache
from dataclasses import replace


@pytest.mark.parametrize('voice_fails', [False, True])
def test_both_identity_lanes_start_without_waiting_for_the_other(voice_fails):
    barrier=Barrier(2, timeout=3)
    calls=[]
    def identify(samples, **kwargs):
        calls.append('voice'); barrier.wait()
        if voice_fails:raise RuntimeError('voice backend failed')
        return 'track', 'person', .9
    def predict(pcm, speakers):
        calls.append('mlp'); barrier.wait()
        return {'status':'uncertain','ranking':[], 'matched':''}
    speakers=SimpleNamespace(lock=RLock(),identify_timed=identify,model_id='test',last_match={})
    personal=SimpleNamespace(predict=predict,store=SimpleNamespace(
        revision=lambda:(1,1),settings=lambda:{'enabled':True},targets=lambda:[]))
    service=ParallelIdentity(speakers,personal)
    s=SpeechSegment(np.zeros(32000,dtype='float32'),'a',0,2000)
    try:
        result=service.identify(s)
        prediction=service.predict(b'\0\0'*32000).result(timeout=3)
        assert prediction['status']=='uncertain'
        assert result.voiceprint_id == ('' if voice_fails else 'person')
        service.identify(s)
        assert sorted(calls)==['mlp','voice']  # exact scope reused, not re-enrolled
        assert service.start(SpeechSegment(s.samples,'mix',0,2000,overlap=True)) is None
    finally:service.close()


def test_feature_extraction_is_shared_while_voice_bank_lock_is_held():
    calls=[]
    class Encoder:
        def embed(self,samples):calls.append(len(samples));return [1.,2.]
    speakers=LocalSpeakers.__new__(LocalSpeakers)
    speakers.model_id='test';speakers.extractor=Encoder()
    speakers.feature_lock=RLock();speakers.feature_cache={};speakers.lock=RLock()
    samples=np.ones(32000,dtype='float32')/32768
    personal=PersonalSpeakers.__new__(PersonalSpeakers)
    # MLP can obtain common features even while the independent voice decision
    # holds its bank lock; a single real model extraction serves both paths.
    with ThreadPoolExecutor(max_workers=2) as pool:
        with speakers.lock:
            mlp=pool.submit(personal.make_features, b'\1\0'*32000, speakers)
            assert mlp.result(timeout=3)[1]==[2.0]
        voice=pool.submit(speakers.embedding,samples)
        assert voice.result(timeout=3)==[1.,2.]
    assert calls==[32000]
    assert speakers.embedding(samples[:8000]) is None  # ordinary voice policy unchanged


def test_only_closed_clean_segments_or_long_checkpoints_are_prefetched():
    service=ParallelIdentity(None,None)
    calls=[]
    service.start=lambda s:calls.append((s.track_id,s.start_ms,s.end_ms)) or True
    try:
        a=SpeechSegment(np.zeros(32000),'a',0,2000)
        b=SpeechSegment(np.zeros(16000),'b',2000,3000)
        service.offer((a,b),3000)
        service.offer((a,b),3100)
        assert calls==[('a',0,2000)]  # A finishes while B continues; no repeated A
        long=SpeechSegment(np.zeros(128000),'b',2000,10000)
        service.offer((long,),10000)
        assert calls[-1]==('b',2000,8000)
        service.offer((long,),10100)
        assert len(calls)==2
    finally:service.close()


def continuity_fixture():
    calls=[]
    epoch=[1]
    def identify(samples, **kw):
        calls.append(('voice',len(samples)))
        return 'known-person', 'person', .9
    def predict(pcm,speakers):
        calls.append(('mlp',len(pcm)//2))
        return {'status':'uncertain','matched':'','ranking':[]}
    speakers=SimpleNamespace(lock=RLock(),identify_timed=identify,model_id='test',last_match={},threshold=.5)
    personal=SimpleNamespace(predict=predict,store=SimpleNamespace(
        revision=lambda:(1,epoch[0]),settings=lambda:{'enabled':True},targets=lambda:[]))
    audio=SessionAudio();audio.append(b'\1\0'*128000)
    cache=SegmentationCache(audio,SimpleNamespace(analyze=lambda samples,start:
        (SpeechSegment(samples,'a',start,start+len(samples)//16),)))
    cache.analyze(np.ones(96000,dtype='float32')/32768,0)
    service=ParallelIdentity(speakers,personal,segments=cache)
    return service,cache,calls,epoch


def test_asr_tail_reuses_both_identity_lanes_and_keeps_new_audio_bounds():
    service,cache,calls,_=continuity_fixture()
    try:
        first=cache.for_range(0,6000)[0]
        service.identify(first)
        final=cache.for_range(0,6600)[0]
        result=service.identify(final)
        prediction=service.predict(b'\1\0'*105600,0,6600,track=final.track_id)
        prediction.result(timeout=2)
        assert sorted(calls)==[('mlp',96000),('voice',96000)]
        assert result.end_ms==6600 and len(result.samples)==105600
        assert result.voiceprint_id=='person' and '沿用' in result.identity_note
        # A transcript trimmed to the word boundaries still belongs to this run.
        assert service.predict(b'\1\0'*96000,100,6100,track=final.track_id) is prediction
    finally:service.close()


def test_pending_identity_is_shared_without_starting_a_second_tail_job():
    from threading import Event
    service,cache,calls,_=continuity_fixture()
    entered,release=Event(),Event()
    original=service.speakers.identify_timed
    def slow(samples,**kw):
        entered.set();assert release.wait(3);return original(samples,**kw)
    service.speakers.identify_timed=slow
    try:
        first=service.start(cache.for_range(0,6000)[0])
        assert entered.wait(2)
        tail=service.start(cache.for_range(0,6600)[0])
        assert first is tail
        release.set();tail.result(timeout=2)
        assert len([x for x in calls if x[0]=='voice'])==1
    finally:release.set();service.close()


@pytest.mark.parametrize('middle', ['overlap','other_voice','unobserved'])
def test_reuse_never_bridges_missing_evidence_overlap_or_another_person(middle):
    service,cache,calls,_=continuity_fixture()
    try:
        first=cache.for_range(0,6000)[0];service.identify(first)
        tail=SpeechSegment(np.ones(25600,dtype='float32')/32768,first.track_id,6400,8000)
        if middle=='unobserved':cache.windows=[(0,6000,(first,)),(6400,8000,(tail,))]
        else:
            parts=[first,tail]
            parts.insert(1,SpeechSegment(np.zeros(6400),
                first.track_id if middle=='overlap' else 'another',6000,6400,overlap=middle=='overlap'))
            cache.windows=[(0,8000,tuple(parts))]
        service.identify(tail)
        assert len([x for x in calls if x[0]=='voice'])==2
    finally:service.close()


def test_short_pause_without_change_does_not_force_another_identity_job():
    service,cache,calls,_=continuity_fixture()
    try:
        first=cache.for_range(0,6000)[0];service.identify(first)
        tail=SpeechSegment(np.ones(25600,dtype='float32')/32768,first.track_id,6400,8000)
        cache.windows=[(0,8000,(first,tail))]  # observed silence, not missing analysis
        service.identify(tail)
        assert len([x for x in calls if x[0]=='voice'])==1
    finally:service.close()


def test_threshold_and_mlp_label_changes_invalidate_only_the_affected_lane():
    service,cache,calls,epoch=continuity_fixture()
    try:
        first=cache.for_range(0,6000)[0];service.identify(first)
        service.predict(b'\1\0'*96000,0,6000,track=first.track_id).result(timeout=2)
        epoch[0]+=1
        final=cache.for_range(0,6600)[0]
        service.identify(final)
        service.predict(b'\1\0'*105600,0,6600,track=final.track_id).result(timeout=2)
        assert len([x for x in calls if x[0]=='voice'])==1
        assert len([x for x in calls if x[0]=='mlp'])==2
        service.speakers.threshold=.6
        service.identify(final)
        assert len([x for x in calls if x[0]=='voice'])==2
    finally:service.close()
