from __future__ import annotations

import asyncio
import json

import numpy as np

from jshi.app.voice import VoiceConversation
from jshi.models import ModelResponse
from jshi.models.prompt import build_system, build_user
from jshi.voice.jev import VoiceJEV
from jshi.voice.local import LocalSpeakers
from jshi.voice.volc import Transcript
from tests.test_voice import FakeCloud, process


def test_turn_diagnostics_include_each_actual_jev_prompt(process):
    requests = []
    class Judge:
        name = 'captured-jev'
        def generate(self, request):
            requests.append(request)
            background = any(item['text'] == '电视解说' for item in json.loads(request.input_text)['batch'])
            return ModelResponse(model=self.name, text=json.dumps({'items': [{'n': 1,
                'to_jiangshi': 'no' if background else 'yes',
                'relevance': 'unrelated' if background else 'related'}]}))
    async def run():
        messages = []
        async def send(message): messages.append(message)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, jev=VoiceJEV(Judge()), input_pause_s=.01)
        try:
            await c.accept(Transcript('电视解说', 'A', 0, 1800, True))
            await asyncio.wait_for(c.queue.join(), 3)
            assert not process.cognition.requests
            await c.accept(Transcript('匠石你好', 'B', 2000, 3800, True))
            await asyncio.wait_for(c.queue.join(), 3)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            assert len(requests) == 2
            await c.diagnostics('prompt', purpose='voice_jev')
            body = messages[-1]['text']
            assert body.count('[voice_jev · captured-jev]') == 2
            for request in requests:
                assert build_system(request) in body and build_user(request) in body
            assert '[subject_activity' not in body
            await c.diagnostics('prompt', part='user', purpose='voice_jev')
            assert 'system\n' not in messages[-1]['text']
            assert all(build_user(request) in messages[-1]['text'] for request in requests)
            assert all(call['prompt_requested'] for call in c.last_debug['timing']['voice']['jev_calls'])
        finally:
            await c.close()
    asyncio.run(run())


def test_rule_jev_diagnostics_do_not_invent_a_model_prompt(process):
    async def run():
        messages = []
        async def send(message): messages.append(message)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, input_pause_s=.01)
        try:
            await c.accept(Transcript('匠石你好', 'A', 0, 1800, True))
            await asyncio.wait_for(c.queue.join(), 3)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            await c.diagnostics('prompt', purpose='voice_jev')
            assert '未发出模型请求' in messages[-1]['text']
        finally:
            await c.close()
    asyncio.run(run())


def test_ten_consistent_short_segments_build_only_a_temporary_voice(process, tmp_path):
    speakers = LocalSpeakers(None, 'test', tmp_path / 'prints.json', process.profiles)
    speakers.embedding = lambda samples: [1., 0.]
    pcm = np.zeros(32000)
    for i in range(2):
        track, vpid, _ = speakers.identify_timed(pcm, start_ms=i * 2000, end_ms=(i + 1) * 2000)
        assert track.startswith('pending-') and not vpid and speakers.last_match['tentative']
    # Replaying the same source interval cannot grow the pending voice.
    speakers.identify_timed(pcm, start_ms=2000, end_ms=4000)
    assert speakers.pending_tracks[0]['count'] == 2
    for i in range(2, 10):
        track, vpid, _ = speakers.identify_timed(pcm, start_ms=i*2000, end_ms=(i+1)*2000)
        if i < 9:
            assert speakers.last_match['tentative'] and not speakers.tracks
    assert track.startswith('pending-') and not vpid
    assert len(speakers.track_samples[track]) == 10 and not speakers.known
    assert not speakers.last_match['tentative']


def test_pending_voice_cannot_compete_with_registered_voice(process, tmp_path):
    speakers = LocalSpeakers(None, 'test', tmp_path / 'prints.json', process.profiles, threshold=.8)
    speakers.embedding = lambda samples: [1., 0.]
    speakers.enroll_samples([bytes(96000)], 'lux-id', user_labeled=True)
    other = [.43, (1 - .43 ** 2) ** .5]
    speakers.embedding = lambda samples: other
    track, vpid, _ = speakers.identify_timed(np.zeros(48000), start_ms=0, end_ms=3000)
    assert track.startswith('pending-') and not vpid
    speakers.threshold = .3
    again, vpid, _ = speakers.identify_timed(np.zeros(48000), start_ms=3000, end_ms=6000)
    assert again == track and not vpid
    assert speakers.last_match['tentative']
    assert speakers.last_match['collection']['count'] == 2
    assert speakers.last_match['candidates'][0]['object_id'] == 'lux-id'


def test_close_registered_and_temporary_matches_do_not_create_another_visitor(process, tmp_path):
    from jshi.recognition import ObjectProfile
    process.profiles.create(ObjectProfile('visitor-id', '未命名访客 41', source='voice_anonymous'))
    speakers = LocalSpeakers(None, 'test', tmp_path / 'prints.json', process.profiles, threshold=.3)
    speakers.embedding = lambda samples: [1., 0., 0.]
    speakers.known = {
        'lux': {'object_id': 'lux-id', 'embedding': [.799, (1-.799**2)**.5, 0.]},
        'visitor': {'object_id': 'visitor-id', 'embedding': [.742, 0., (1-.742**2)**.5], 'expires_at': speakers.clock()+100},
    }
    track, vpid, _ = speakers.identify_timed(np.zeros(64000), start_ms=0, end_ms=4000)
    assert track == 'known-lux-id' and vpid
    assert set(speakers.tracks) == {'known-lux-id'}
    assert len(speakers.last_match['candidates']) == 1
    assert [oid for score, oid in speakers.rank_vector([1., 0., 0.])] == ['lux-id']


def test_manual_enrollment_retires_only_selected_temporary_voice(process, tmp_path):
    from jshi.voice.audio_buffer import SessionAudio
    from jshi.recognition import ObjectProfile
    async def run():
        speakers = LocalSpeakers(None, 'test', tmp_path / 'prints.json', process.profiles)
        speakers.embedding = lambda samples: [1., 0.]
        speakers.tracks['temp'] = [1., 0.]
        speakers.last_embedding['temp'] = [1., 0.]
        process.profiles.create(ObjectProfile('other-id', '未命名访客 999', source='voice_anonymous'))
        speakers.known['other'] = {'object_id': 'other-id', 'embedding': [0., 1.], 'expires_at': speakers.clock()+100}
        audio = SessionAudio(); audio.append(bytes(96000))
        events = []
        async def send(message): events.append(message)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, local_speakers=speakers, sample_source=audio, input_pause_s=.01)
        try:
            await c.accept(Transcript('匠石，这是一段清晰发言。', 'A', 0, 3000, True, speaker_cluster_id='temp'))
            await asyncio.wait_for(c.queue.join(), 3)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            iid = next(iter(c.input_records))
            old_id = c.source_speakers[iid].object_id
            assert any(e['object_id'] == old_id for e in speakers.known.values())
            old_profile = process.profiles.get(old_id)
            await c.enroll_inputs([iid], 'lux')
            assert events[-1]['type'] == 'enrollment'
            assert not any(e['object_id'] == old_id for e in speakers.known.values())
            assert {e['object_id'] for e in speakers.known.values()} == {'lux-id', 'other-id'}
            assert process.profiles.get(old_id) == old_profile
            assert 'temp' not in speakers.tracks
            assert c.scene[0].speaker.object_id == 'lux-id'
            metadata, _ = c.review_material(tuple(c.scene), ())
            assert '用户已手动将本句声音关联为' in metadata
        finally:
            await c.close()
    asyncio.run(run())


def test_recognized_temporary_voice_can_introduce_its_existing_name(process):
    from jshi.recognition import CarrierEntry, ObjectProfile
    from jshi.voice.identity import VoiceIdentities
    process.profiles.create(ObjectProfile('temp-id', '未命名访客 41', source='voice_anonymous',
        carriers=(CarrierEntry('voiceprint', 'local:test:temp'),)))
    ids = VoiceIdentities(process.profiles, 'stone', 'test')
    before = process.profiles.get('temp-id')
    old = ids.resolve('A', voiceprint_id='local:test:temp', cluster_id='temp')
    assert old.status == 'recognized'
    introduced, _ = ids.introduce('A', 'lux', cluster_id='temp')
    assert introduced.object_id == 'lux-id'
    assert ids.associations['temp-id'].object_id == 'lux-id'
    assert process.profiles.get('temp-id') == before


def test_clear_anonymous_cluster_reaches_conversation_and_retains_internal_person(process, tmp_path):
    from jshi.voice.audio_buffer import SessionAudio
    from jshi.voice.identity import VoiceIdentities
    speakers = LocalSpeakers(None, 'test', tmp_path / 'prints.json', process.profiles)
    speakers.embedding = lambda samples: [1., 0.]
    audio = SessionAudio()
    audio.append(bytes(960000))
    ids = VoiceIdentities(process.profiles, 'stone', 'session')
    first = audio.match(Transcript('第一句', 'cloud-A', 0, 3000, True), speakers)
    second = audio.match(Transcript('第二句', 'cloud-B', 3000, 6000, True), speakers)
    a = ids.resolve(first.track_id, cluster_id=first.speaker_cluster_id, tentative=first.identity_tentative)
    b = ids.resolve(second.track_id, cluster_id=second.speaker_cluster_id, tentative=second.identity_tentative)
    assert first.speaker_cluster_id == second.speaker_cluster_id
    assert a.object_id == b.object_id != 'lux-id'
    assert a.method == b.method == 'voice_continuity'
    assert a.label.startswith('待定声音') and process.profiles.get(a.object_id) is None
    for i in range(2, 10):
        t = audio.match(Transcript('继续', 'cloud-C', i*3000, (i+1)*3000, True), speakers)
        stable = ids.resolve(t.track_id, cluster_id=t.speaker_cluster_id, tentative=t.identity_tentative)
    assert stable.object_id == a.object_id and stable.method == 'anonymous_voice_cluster'
    assert speakers.remember_anonymous(t.speaker_cluster_id, stable.object_id)
    assert all('expires_at' in entry for entry in speakers.known.values())


def test_subject_nicknames_are_saved_in_portrait_for_the_identified_speaker(process):
    from dataclasses import replace
    from types import SimpleNamespace
    async def run():
        async def send(message): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send)
        try:
            known, _ = c.identities.introduce('A', 'lux')
            c.note_address(known, 'Hello hello jiang，我来了', 'nickname', addressed=True)
            c.note_address(known, '姜，听到了吗', 'nickname-2', addressed=True)
            portrait = process.profiles.address_line('lux-id')
            assert '常称匠石为' in portrait and 'jiang' in portrait and '「姜」' in portrait
            c.note_address(replace(known, method='unassigned_audio'), '我叫你小石头', 'unassigned', addressed=True)
            assert '小石头' not in process.profiles.address_line('lux-id')
            lines = process._person_experience_lines([SimpleNamespace(id='person-experience:lux-id', object_id='lux-id',
                source='person_experience', content='常主动聊天。')], {'lux-id': 'P1'})
            assert lines == ['P1（lux）：常主动聊天。']
            assert 'person-experience:' not in lines[0]
        finally:
            await c.close()
    asyncio.run(run())


def test_eight_supporting_samples_and_two_misses_use_distribution_without_training(process, tmp_path):
    speakers = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles, threshold=.6)
    speakers.embedding = lambda _: [1., 0.]
    speakers.enroll_samples([bytes(96000)], 'lux-id', user_labeled=True)
    bank_before = json.dumps(speakers.known, sort_keys=True)
    for i, score in enumerate([.63]*8 + [.51]*2):
        speakers.embedding = lambda _, value=score: [value, (1-value**2)**.5]
        track, vpid, confidence = speakers.identify_timed(np.zeros(32000), start_ms=i*2000, end_ms=(i+1)*2000)
        if i < 9:
            assert not vpid and speakers.last_match['tentative']
    assert vpid and track == 'known-lux-id'
    assert abs(confidence-.63) < 1e-6
    assert speakers.last_match['collection']['support_fraction'] == .8
    assert json.dumps(speakers.known, sort_keys=True) == bank_before


def test_ten_samples_do_not_confirm_an_inconsistent_mixture(process, tmp_path):
    speakers = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles)
    for i in range(10):
        speakers.embedding = lambda _, index=i: [1., 0.] if index < 5 else [.5, .75**.5]
        speakers.identify_timed(np.zeros(32000), start_ms=i*2000, end_ms=(i+1)*2000)
    assert speakers.last_match['collection']['count'] == 10
    assert not speakers.last_match['collection']['stable']
    assert speakers.last_match['tentative'] and not speakers.tracks and not speakers.known


def test_pending_voice_can_chat_and_manual_name_confirmation_retains_only_voice_scope(process, tmp_path):
    from jshi.voice.audio_buffer import SessionAudio
    async def run():
        speakers = LocalSpeakers(None, 'test', tmp_path/'prints.json', process.profiles)
        speakers.embedding = lambda _: [1., 0.]
        audio = SessionAudio(); audio.append(bytes(96000))
        messages = []
        async def send(message): messages.append(message)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, local_speakers=speakers, sample_source=audio, input_pause_s=.01)
        try:
            t = audio.match(Transcript('匠石，你好。', 'A', 0, 3000, True), speakers)
            await c.accept(t)
            await asyncio.wait_for(c.queue.join(), 3)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            assert process.cognition.requests
            pending = c.scene[0].speaker
            assert pending.label.startswith('待定声音')
            assert process.profiles.get(pending.object_id) is None and not speakers.known
            await c.enroll_inputs(list(c.input_records), 'lux')
            assert messages[-1]['type'] == 'enrollment'
            assert c.scene[0].speaker.object_id == 'lux-id'
            assert not speakers.pending_tracks
            assert {entry['object_id'] for entry in speakers.known.values()} == {'lux-id'}
            assert process.profiles.get(pending.object_id) is None
        finally:
            await c.close()
    asyncio.run(run())


def test_withdrawn_voice_cannot_return_from_a_stale_bank_save(process, tmp_path):
    from jshi.recognition import ObjectProfile
    path = tmp_path/'prints.json'
    speakers = LocalSpeakers(None, 'test', path, process.profiles)
    speakers.embedding = lambda _: [1., 0.]
    process.profiles.create(ObjectProfile('deleted-id', 'other'))
    speakers.known = {
        'keep': {'object_id': 'lux-id', 'embedding': [.8, .6]},
        'remove': {'object_id': 'deleted-id', 'embedding': [1., 0.]},
    }
    speakers._save_bank()
    path.with_suffix('.withdrawn.json').write_text(json.dumps({'model_id': 'test', 'keys': ['remove']}), encoding='utf-8')
    ranking = speakers.rank_known(np.zeros(48000))
    assert [r['label'] for r in ranking] == ['lux']
    speakers.known['remove'] = {'object_id': 'deleted-id', 'embedding': [1., 0.]}
    speakers._save_bank()
    assert set(json.loads(path.read_text(encoding='utf-8'))['entries']) == {'keep'}
    restored = LocalSpeakers(None, 'test', path, process.profiles)
    assert set(restored.known) == {'keep'}
