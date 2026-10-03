from __future__ import annotations

import asyncio
import json
import numpy as np
import pytest

from jshi.app.voice import VoiceConversation
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.local import LocalSpeakers
from jshi.voice.volc import Transcript, TranscriptAssembler
from tests.test_voice import process, FakeCloud


def test_anonymous_objects_do_not_fragment_when_name_matching_is_strict(process, tmp_path):
    s=LocalSpeakers(None,'test',tmp_path/'prints.json',process.profiles,threshold=.77)
    s.embedding=lambda a:[1.,0.]
    first,_,_=s.identify([0]*48000)
    s.embedding=lambda a:[.7,(1-.7**2)**.5]
    assert s.identify([0]*48000)[0]==first
    assert len(s.tracks)==1
    assert len(s.track_samples[first])==2
    # A short, clearly different observation cannot create another visitor.
    s.embedding=lambda a:[-1.,0.]
    assert s.identify([0]*32000)[0].startswith('unidentified-')
    assert len(s.tracks)==1


def test_different_anonymous_voice_needs_repeated_long_observations(process, tmp_path):
    s=LocalSpeakers(None,'test',tmp_path/'prints.json',process.profiles)
    s.embedding=lambda a:[1.,0.]
    first=s.identify([0]*48000)[0]
    s.embedding=lambda a:[0.,1.]
    assert s.identify([0]*48000)[0].startswith('unidentified-')
    assert len(s.tracks)==1
    second=s.identify([0]*48000)[0]
    assert second!=first and second.startswith('speaker-') and len(s.tracks)==2
    # Equally close to two voices: leave unassigned, not a third visitor.
    s.embedding=lambda a:[1.,1.]
    assert s.identify([0]*48000)[0].startswith('unidentified-')
    assert len(s.tracks)==2


def test_uncertain_audio_cannot_bind_a_previous_cloud_track_embedding(process, tmp_path):
    s=LocalSpeakers(None,'test',tmp_path/'prints.json',process.profiles)
    s.last_embedding['0']=[1.,0.]
    audio=SessionAudio(); audio.append(bytes(32000))
    t=audio.match(Transcript('你好','0',0,1000,True),s)
    assert t.identity_uncertain and not t.speaker_cluster_id
    assert not s.bind('0','lux-id')
    s.last_embedding['0']=[1.,0.]
    t=audio.match(Transcript('重叠','0',0,1000,True,overlap=True),s)
    assert t.identity_uncertain and '0' not in s.last_embedding


def test_selected_samples_create_normalized_persistent_centroid(process, tmp_path):
    speakers = LocalSpeakers(None, 'manual-test', tmp_path/'prints.json', process.profiles)
    lengths = []
    def embedding(samples):
        lengths.append(len(samples))
        return [2., 0.]
    speakers.embedding = embedding
    stats = speakers.enroll_samples([bytes(64000), bytes(64000)], 'lux-id')
    assert lengths == [32000, 32000]
    assert stats == {'clips': 2, 'embeddings': 2, 'seconds': 4.0, 'min_similarity': 1.0, 'enrollment_threshold': .8}
    entry = next(iter(speakers.known.values()))
    assert entry['embedding'] == [1., 0.] and entry['object_id'] == 'lux-id'
    restored = LocalSpeakers(None, 'manual-test', tmp_path/'prints.json', process.profiles)
    restored.embedding = lambda samples: [1., 0.]
    assert restored.identify(np.ones(32000))[1].startswith('local:manual-test:')


def test_matching_slider_changes_recognition_and_survives_restart(process, tmp_path):
    path=tmp_path/'prints.json'
    speakers=LocalSpeakers(None,'model',path,process.profiles)
    speakers.embedding=lambda a:[1.,0.]
    track,_,_=speakers.identify([0]*48000)
    assert speakers.bind(track,'lux-id')
    speakers.embedding=lambda a:[.64, (.5904)**.5]
    assert not speakers.identify([])[1]
    speakers.set_threshold(.60)
    assert speakers.identify([])[1]
    assert speakers.margin==.08 and speakers.enrollment_threshold==.8
    assert LocalSpeakers(None,'model',path,process.profiles).threshold==.60
    assert LocalSpeakers(None,'other-model',path,process.profiles).threshold==.65
    for bad in (.1,1,float('nan'),True,'0.6'):
        with pytest.raises(ValueError):speakers.set_threshold(bad)
    assert speakers.threshold==.60


def test_different_people_selected_together_are_not_registered(process, tmp_path):
    speakers = LocalSpeakers(None, 'manual-test', tmp_path/'prints.json', process.profiles)
    vectors = iter(([1., 0.], [0., 1.]))
    speakers.embedding = lambda samples: next(vectors)
    with pytest.raises(ValueError, match='一致性不足') as error:
        speakers.enroll_samples([bytes(64000), bytes(64000)], 'lux-id')
    assert error.value.sample_indices == (0, 1)
    assert not speakers.known and not speakers.path.exists()


def test_short_clips_cannot_hide_multiple_people_by_concatenation(process, tmp_path):
    speakers = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles)
    with pytest.raises(ValueError, match='不足 1.5') as error:
        speakers.enroll_samples([bytes(16000)] * 8, 'lux-id')
    assert error.value.sample_indices == tuple(range(8))
    assert not speakers.known


def test_long_clip_checks_internal_speaker_changes(process, tmp_path):
    speakers = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles)
    vectors = iter(([1., 0.], [0., 1.]))
    speakers.embedding = lambda samples: next(vectors)
    with pytest.raises(ValueError, match='第 1 段') as error:
        speakers.enroll_samples([bytes(192000)], 'lux-id')
    assert error.value.sample_indices == (0,)
    assert not speakers.known


def test_manual_selection_uses_retained_audio_and_does_not_bind_cloud_track(process, tmp_path):
    async def run():
        speakers = LocalSpeakers(None, 'manual-test', tmp_path/'prints.json', process.profiles)
        speakers.embedding = lambda samples: [1., 0.]
        audio = SessionAudio(seconds=3)
        audio.append(bytes(96000))
        events = []
        async def send(m): events.append(m)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, local_speakers=speakers, sample_source=audio, input_pause_s=.02)
        try:
            await c.accept(Transcript('这是一段声音', '7', 0, 3000, True))
            await asyncio.wait_for(c.queue.join(), 1)
            await asyncio.wait_for(c.turn_queue.join(), 2)
            iid = next(iter(c.input_records))
            audio.append(bytes(128000))  # original sample leaves rolling ASR buffer
            assert audio.segment(0, 2000) is None
            await c.enroll_inputs([iid], 'lux')
            assert events[-1]['type'] == 'enrollment'
            assert c.scene[0].speaker.object_id == 'lux-id'
            assert c.scene[0].speaker.method == 'manual_annotation'
            assert c.identities.resolve('7').status == 'unknown'
            assert next(iter(speakers.known.values()))['object_id'] == 'lux-id'
            await c.enroll_inputs(['expired'], 'lux')
            assert events[-1]['type'] == 'enrollment_error'
        finally:
            await c.close()
        assert not c.input_records
    asyncio.run(run())


def test_overlapping_sample_cannot_be_enrolled(process, tmp_path):
    async def run():
        speakers = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles)
        audio = SessionAudio(); audio.append(bytes(64000))
        events = []
        async def send(m): events.append(m)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, local_speakers=speakers, sample_source=audio)
        try:
            await c.accept(Transcript('混合声音', '7', 0, 2000, True, overlap=True))
            await c.enroll_inputs(list(c.input_records), 'lux')
            assert events[-1]['type']=='enrollment_error' and '重叠' in events[-1]['text']
            assert not speakers.known
        finally: await c.close()
    asyncio.run(run())


def test_prompt_sections_and_call_selection_are_actual_saved_records(process):
    async def run():
        events=[]
        async def send(m): events.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send,input_pause_s=.02)
        try:
            await c.accept(Transcript('你好','A',0,2000,True))
            await asyncio.wait_for(c.queue.join(),1)
            await asyncio.wait_for(c.turn_queue.join(),2)
            activity=c.last_debug['activity_id']
            process.step_inputs.append_call(subject_id='stone', activity_id=activity, purpose='write_zone', model='test',
                system_text='【测试区块】\n只有实际记录\n【另一块】\n尾部\nJSON Schema：{}', user_text='独立写场输入')
            await c.diagnostics('prompt', activity_id=activity, purpose='write_zone', part='测试区块')
            assert '只有实际记录' in events[-1]['text'] and '尾部' not in events[-1]['text']
            assert '测试区块' in events[-1]['sections']
            await c.diagnostics('prompt', purpose='write_zone', part='user')
            assert '独立写场输入' in events[-1]['text']
            for section in ('input','scene','identity','delivery','jev','response','timing'):
                await c.diagnostics(section)
                json.loads(events[-1]['text'])
            assert 'queue_wait_ms' in events[-1]['text']
        finally: await c.close()
    asyncio.run(run())


def test_old_merged_audio_with_small_silence_gaps_is_not_a_new_turn():
    assembler=TranscriptAssembler()
    rows=[{'text':'妈，我出去玩的那只呢','start_time':209900,'end_time':213800,'speaker_id':'0','definite':True},
          {'text':'什么叫你出去玩','start_time':214800,'end_time':215700,'speaker_id':'7','definite':True},
          {'text':'我出去玩带的那只笔呢','start_time':215800,'end_time':217800,'speaker_id':'0','definite':True}]
    assert len(assembler.accept({'result':{'utterances':rows}}))==3
    merged={'text':'妈我出去玩的那只呢什么叫你出去玩我出去玩带的那只笔呢','start_time':209900,'end_time':217800,'speaker_id':'0','definite':True}
    assert assembler.accept({'result':{'utterances':[merged]}})==()
