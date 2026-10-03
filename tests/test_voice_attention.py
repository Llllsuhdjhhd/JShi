from dataclasses import replace
from threading import RLock

from jshi.core.envelope import SpeakerEvidence
from jshi.voice.attention import ConversationAttention
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.volc import Transcript


def test_focus_expires_and_does_not_claim_identity():
    now = [0]
    attention = ConversationAttention(clock=lambda: now[0])
    lux = SpeakerEvidence('A', 'lux')
    other = SpeakerEvidence('B', 'other')
    attention.engage(('lux',))
    assert attention.hint(lux)['weight'] > attention.hint(other)['weight']
    assert attention.hint(lux)['identity_uncertain']
    now[0] = 91
    assert attention.hint(lux)['weight'] == 1
    assert not attention.hint(lux)['focused']


def test_audio_offsets_eviction_and_cloud_identity_without_changing_track():
    audio = SessionAudio(seconds=2)
    audio.append(b'\0\0' * 16000)
    audio.append(b'\1\0' * 32000)
    assert audio.segment(0, 1000) is None
    assert audio.segment(1000, 3000) == b'\1\0' * 32000
    assert audio.segment(3000, 4000) is None
    class Speakers:
        lock = RLock()
        last_embedding = {}
        calls = 0
        def identify(self, samples):
            self.calls += 1
            assert len(samples) == 32000
            self.last_embedding['known'] = [1., 0.]
            return 'known', 'local:model:id', .9
    speakers = Speakers()
    t = Transcript('你好', 'cloud-0', 1000, 3000, True)
    matched = audio.match(t, speakers)
    assert matched.track_id == 'cloud-0' and matched.voiceprint_id == 'local:model:id'
    assert speakers.last_embedding['cloud-0'] == [1., 0.]
    assert audio.match(replace(t, overlap=True), speakers).voiceprint_id == ''
    assert audio.match(replace(t, final=False), speakers).voiceprint_id == ''
    assert speakers.calls == 1
