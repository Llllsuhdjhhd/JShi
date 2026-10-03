from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import gzip
import json
import struct
from threading import Event
from types import SimpleNamespace

import pytest

from jshi.core import SubjectState
from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import ModelRequest, ModelResponse, ObjectAssessment
from jshi.models.prompt import build_user
from jshi.recognition import CarrierEntry, ObjectProfile, ObjectProfileRepository
from jshi.subject import SubjectProcess, SubjectRepository, HistoryKind
from jshi.voice.config import VoiceConfig, VoiceTemplate
from jshi.voice.delivery import DeliveryTracker
from jshi.voice.identity import VoiceIdentities
from jshi.voice.jev import VoiceJEV, explicit_name
from jshi.voice.local import LocalSpeakers, cosine
from jshi.voice.volc import VolcVoice, Transcript, TranscriptAssembler, pack_asr, unpack_asr
from jshi.app.voice import VoiceConversation


class Model:
    name = "test"
    def __init__(self):
        self.requests = []
    def generate(self, request):
        self.requests.append(request)
        return ModelResponse(text="上午去古镇。下午去博物馆。", model=self.name)
    def generate_stream(self, request, on_reply=None):
        result = self.generate(request)
        if on_reply:
            on_reply(result.text)
        return result


@pytest.fixture
def process(tmp_path):
    identities = IdentityRepository(tmp_path / "identity.json")
    identities.create(IdentityProfile("stone", "匠石", "测试", "我是匠石"))
    p = SubjectProcess(SubjectRepository(tmp_path / "subjects.db"), identities, Model())
    p.profiles.create(ObjectProfile("lux-id", "lux", status="confirmed", channel="family-mic"))
    return p


def envelope(p, text="你好", track="A"):
    identities = VoiceIdentities(p.profiles, "stone", "session", p.repository)
    evidence = identities.resolve(track)
    return InputEnvelope("session", "family-mic", (InputPart("text", text),), evidence), identities


def test_unknown_audio_converses_without_using_device_owner(process):
    e, ids = envelope(process)
    result = process.experience("stone", e.text, envelope=e, resolved_speaker=ids.candidate(e.speaker))
    assert result.speaker.object_id != "lux-id"
    assert result.speaker.reason == "voice_unknown"
    assert process.profiles.get(result.speaker.object_id).status == "provisional"
    req = process.cognition.requests[0]
    assert req.input_text == "你好"
    assert "声音身份尚未明确" in build_user(req)
    assert "不要问你是" not in build_user(req)
    facts = process.repository.list_history("stone", HistoryKind.FACT)
    inbound = next(r for r in facts if r.event_type == "external_input")
    assert inbound.content["envelope"]["speaker"]["status"] == "unknown"
    ledger = process.activity_ledger.list_experiences("stone")
    assert any("实际交付待播放器反馈" in (s.text_raw or "") for s in ledger)


def test_final_and_matching_envelope_required(process):
    e, ids = envelope(process)
    with pytest.raises(ValueError):
        process.experience("stone", "别的话", envelope=e, resolved_speaker=ids.candidate(e.speaker))
    with pytest.raises(ValueError):
        process.experience("stone", e.text, envelope=replace(e, final=False), resolved_speaker=ids.candidate(e.speaker))
    with pytest.raises(ValueError):
        process.experience("stone", "你好")
    with pytest.raises(ValueError):
        process.experience("stone", e.text, envelope=e, resolved_speaker=replace(ids.candidate(e.speaker), actor_object_id="lux-id"))


def test_early_verbal_happens_before_write_zone(process):
    e, ids = envelope(process)
    order = []
    class Writer(Model):
        def generate(self, req):
            order.append("write")
            assert "没有播放完成反馈" in req.transport_context
            assert "我准备说" in req.persona_user_text
            return ModelResponse(rewritten_context="匠石准备说话。", model="writer")
    process.write_zone = Writer()
    process.experience("stone", e.text, envelope=e, resolved_speaker=ids.candidate(e.speaker),
                       on_verbal=lambda text: order.append("verbal"))
    assert order == ["verbal", "write"]


def test_unknown_cannot_be_confirmed_by_model_assessment(process):
    class Confirmer(Model):
        def generate(self, req):
            return ModelResponse(text="你好", model="test", object_assessment=ObjectAssessment(conclusion="confirm"))
    process.cognition = Confirmer()
    e, ids = envelope(process)
    process.experience("stone", e.text, envelope=e, resolved_speaker=ids.candidate(e.speaker))
    assert process.profiles.get(e.speaker.object_id).status == "provisional"


def test_identity_self_introduction_links_existing_and_records_old_reference(process):
    e, ids = envelope(process)
    updated, note = ids.introduce("A", "lux")
    assert updated.object_id == "lux-id"
    assert e.speaker.object_id in note
    assert ids.resolve("A") == updated
    assert ids.resolve("B").object_id != updated.object_id
    event = process.repository.list_history("stone", HistoryKind.FACT)[-1]
    assert event.content["previous_object_id"] == e.speaker.object_id


def test_same_name_is_not_automatically_bound(process):
    process.profiles.create(ObjectProfile("other-lux", "lux", status="confirmed"))
    e, ids = envelope(process)
    updated, note = ids.introduce("A", "lux")
    assert updated == e.speaker
    assert "多个" in note


def test_old_greeting_name_voiceprint_resolves_existing_lux(process):
    process.profiles.create(ObjectProfile('old-voice-name', 'L U X 你好',
        source='voice_self_introduction', status='provisional'))
    process.profiles.add_carrier('old-voice-name', CarrierEntry('voiceprint','local:model:old'))
    ids = VoiceIdentities(process.profiles, 'stone', 'session')
    assert ids.resolve('0', voiceprint_id='local:model:old').object_id == 'lux-id'
    assert process.profiles.get('old-voice-name').label == 'L U X 你好'


def test_final_speaker_revision_does_not_repeat_input():
    assembler = TranscriptAssembler()
    u = {'text':'我困了。','start_time':100,'end_time':2000,'definite':True,'speaker_id':'0'}
    assert len(assembler.accept({'result':{'utterances':[u]}})) == 1
    assert not assembler.accept({'result':{'utterances':[{**u,'speaker_id':'1'}]}})
    assert assembler.accept({'result':{'utterances':[{**u,'start_time':3000,'end_time':5000}]}})


def test_old_voice_name_samples_do_not_compete_with_same_person(process,tmp_path):
    process.profiles.create(ObjectProfile('old-voice-name','L U X 你好',source='voice_self_introduction'))
    speakers=LocalSpeakers(None,'model',tmp_path/'voices.json',process.profiles)
    speakers.known={'old':{'object_id':'old-voice-name','embedding':[1.,0.]},
                    'lux':{'object_id':'lux-id','embedding':[1.,0.]}}
    speakers.embedding=lambda samples:[1.,0.]
    assert speakers.identify([])[1].startswith('local:model:')


def test_carrier_collision_is_rejected_and_device_is_not_voiceprint(process):
    process.profiles.add_carrier("lux-id", CarrierEntry("voiceprint", "volc:sample"))
    process.profiles.create(ObjectProfile("other", "小明"))
    with pytest.raises(ValueError):
        process.profiles.add_carrier("other", CarrierEntry("voiceprint", "volc:sample"))
    ids = VoiceIdentities(process.profiles, "stone", "session")
    assert ids.resolve("A", voiceprint_id="sample").object_id == "lux-id"
    assert ids.resolve("B").object_id != "lux-id"


def test_delivery_only_acknowledges_played_and_discards_stale():
    tracker = DeliveryTracker()
    d = tracker.begin("第一句。第二句。", "lux")
    tracker.generated(d.reply_id)
    assert tracker.snapshot()["played_text"] == ""
    assert tracker.acknowledge(d.reply_id, 0, "started")
    assert tracker.pause()
    assert tracker.snapshot()["partial_text"] == "第一句。"
    assert tracker.acknowledge(d.reply_id, 0, "completed")
    assert not tracker.acknowledge(d.reply_id, 0, "completed")
    tracker.stop()
    assert not tracker.acknowledge(d.reply_id, 1, "completed")
    assert not tracker.accepts(d.reply_id)
    assert "第一句。" in tracker.context_note()
    fresh = tracker.begin("新回应。", "other")
    assert not tracker.acknowledge(d.reply_id, 0, "started")
    assert tracker.accepts(fresh.reply_id)


def test_completed_playback_can_finish_before_synthesis_final_marker():
    tracker = DeliveryTracker()
    d = tracker.begin("一句。", "lux")
    assert tracker.acknowledge(d.reply_id, 0, "completed")
    assert tracker.snapshot()["state"] != "completed"
    tracker.generated(d.reply_id)
    assert tracker.snapshot()["state"] == "completed"


def test_asr_protocol_and_error_frames():
    frame = pack_asr(b"abcd", audio=True, final=True)
    assert frame[:4] == bytes((0x11, 0x22, 0x01, 0))
    assert gzip.decompress(frame[8:]) == b"abcd"
    body = gzip.compress(json.dumps({"result": {"text": "你好"}}).encode())
    frame = bytes((0x11, 0x93, 0x11, 0)) + struct.pack(">iI", -3, len(body)) + body
    payload, final = unpack_asr(frame)
    assert final and payload["result"]["text"] == "你好"
    with pytest.raises(ValueError):
        unpack_asr(frame[:-1])
    error = b'{"message":"permission denied"}'
    with pytest.raises(RuntimeError, match="45000000"):
        unpack_asr(bytes((0x11, 0xf0, 0x10, 0))+struct.pack(">II",45000000,len(error))+error)


def test_asr_partial_final_dedup_and_multiple_speakers():
    assembler = TranscriptAssembler()
    u = {"text":"你好", "start_time":0,"end_time":1000,"additions":{"speaker_id":"0"},"definite":False}
    assert not assembler.accept({"result":{"utterances":[u]}})[0].final
    u["definite"] = True
    assert assembler.accept({"result":{"utterances":[u]}})[0].final
    assert not assembler.accept({"result":{"utterances":[u]}})
    a = {**u,"start_time":2000,"end_time":4000}
    b = {**u,"start_time":3000,"end_time":4500,"additions":{"speaker_id":"1"}}
    outputs = assembler.accept({"result":{"utterances":[a,b]}})
    assert all(x.overlap for x in outputs)
    assert all(not x.voiceprint_id for x in outputs)


def test_cloud_requests_keep_multispeaker_and_identity_modes_separate():
    multi = VolcVoice(VoiceConfig("secret"), None)
    req = multi.asr_request()["request"]
    assert req["enable_speaker_info"] and req["show_utterances"]
    assert req["ssd_version"] == "200" and req["end_window_size"] == 500
    assert "ssd_mode" not in req
    single = VolcVoice(VoiceConfig("secret", identity_mode="voiceprint", voiceprint_group="family"),None)
    assert single.asr_request()["request"]["ssd_mode"] == 2
    assert multi.tts_request("你好")["req_params"]["audio_params"]["format"] == "pcm"


def test_cloud_history_regrouping_does_not_deliver_old_audio_again():
    assembler = TranscriptAssembler()
    def row(text, start, end, speaker):
        return {"text": text, "start_time": start, "end_time": end,
                "speaker_id": speaker, "definite": True}
    initial = [row("谁说的", 4846900, 4847700, "0"),
               row("没有刚醒累", 4847700, 4852000, "1")]
    assert len(assembler.accept({"result": {"utterances": initial}})) == 2
    revision = row("谁说的没有刚醒累", 4846900, 4852000, "4")
    assert assembler.accept({"result": {"utterances": [revision]}}) == ()
    fresh = row("干啥", 4990000, 4992000, "0")
    assert len(assembler.accept({"result": {"utterances": [fresh]}})) == 1
    # A late history revision remains suppressed after its ranges are pruned.
    assert assembler.accept({"result": {"utterances": [revision]}}) == ()
    assert len(assembler.ranges) == 1


def test_same_packet_overlapping_new_speakers_are_both_preserved():
    assembler = TranscriptAssembler()
    rows = [{"text": "甲在说话", "start_time": 100, "end_time": 2000,
             "speaker_id": "0", "definite": True},
            {"text": "乙也在说话", "start_time": 200, "end_time": 1800,
             "speaker_id": "1", "definite": True}]
    outputs = assembler.accept({"result": {"utterances": rows}})
    assert len(outputs) == 2 and all(t.overlap for t in outputs)


def test_new_overlapping_speaker_in_later_packet_is_not_history_revision():
    assembler = TranscriptAssembler()
    a = {"text": "甲在说话", "start_time": 100, "end_time": 2000,
         "speaker_id": "0", "definite": True}
    b = {"text": "乙也在说话", "start_time": 200, "end_time": 1800,
         "speaker_id": "1", "definite": True}
    assembler.accept({"result": {"utterances": [a]}})
    output = assembler.accept({"result": {"utterances": [b]}})
    assert len(output) == 1 and output[0].overlap


def test_cloud_endpoint_silence_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv('JSHI_VOICE_END_WINDOW_MS', '300')
    config = VoiceConfig.from_env(asr='local', tts='local')
    assert VolcVoice(config, None).asr_request()['request']['end_window_size'] == 300
    with pytest.raises(ValueError):
        VoiceConfig('', end_window_ms=250)


@pytest.mark.parametrize("text,expected",[("我是 lux。","lux"),("我叫lux，今天刚下班。","lux"),("lux 说他是小明", ""),("帮我查天气", "")])
def test_program_only_binds_explicit_self_introductions(text, expected):
    assert explicit_name(text) == expected


def test_jev_explicit_stop_and_unknown_backchannel():
    class Broken:
        def generate(self,req):
            raise AssertionError("should not call model for stop")
    jev = VoiceJEV(Broken())
    assert jev.decide("stone","停一下",{},{}).action == "stop"
    d = {"object_id":"lux","state":"paused"}
    assert VoiceJEV().decide("stone","嗯",{"object_id":"lux","status":"recognized"},d).action == "resume"
    assert VoiceJEV().decide("stone","嗯",{"status":"unknown"},d).action == "resume"
    assert VoiceJEV().decide("stone","我也想去",{},d,overlap=True).action == "resume"
    assert VoiceJEV().decide("stone","我也想去",{},{} ,overlap=True).action == "ignore"


def test_jev_model_failure_keeps_playing_and_other_speaker_needs_judgment():
    class Judge:
        def __init__(self): self.requests=[]
        def generate(self, req):
            self.requests.append(req)
            return ModelResponse(text='{"action":"resume","reason":"旁人交谈"}',model="test")
    judge=Judge()
    d={"object_id":"lux","state":"playing","pending_text":"路线介绍"}
    assert VoiceJEV(judge).decide("stone","今天你吃饭了吗",{"object_id":"other"},d).action=="resume"
    assert len(judge.requests)==1
    class Broken:
        def generate(self,req): raise RuntimeError("timeout")
    assert VoiceJEV(Broken()).decide("stone","等会去哪",{},d).action=="resume"


def test_pending_jev_does_not_pause_audio(process):
    from threading import Event
    entered, release=Event(),Event()
    class Judge:
        def decide(self,*args,**kwargs):
            entered.set(); release.wait(2)
            from jshi.voice.jev import InterruptDecision
            return InterruptDecision("resume","background conversation")
    async def run():
        messages=[]
        async def send(m): messages.append(m)
        c=VoiceConversation(process,"stone",FakeCloud(),send,jev=Judge())
        d=c.delivery.begin("正在继续说。","lux")
        c.delivery.acknowledge(d.reply_id,0,"started")
        try:
            await c.accept(Transcript("你今天吃饭了吗","other",0,2000,True))
            assert await asyncio.to_thread(entered.wait,4)
            assert c.delivery.snapshot()["state"]=="playing"
            assert not any(m["type"] in {"pause","stop"} for m in messages)
            release.set(); await asyncio.wait_for(c.queue.join(),3)
            assert c.delivery.snapshot()["state"]=="playing"
            assert any(m["type"]=="jev" and m["action"]=="resume" for m in messages)
        finally:
            release.set(); await c.close()
    asyncio.run(run())


def test_local_voiceprints_persist_and_ignore_other_model(tmp_path, process):
    class Extractor:
        def create_stream(self): return self
        def accept_waveform(self,**kw): pass
        def input_finished(self): pass
        def is_ready(self,s): return True
        def compute(self,s): return [1.0,0.0]
    path = tmp_path / "voiceprints.json"
    speakers = LocalSpeakers(Extractor(),"model1",path,process.profiles)
    track,vpid,score = speakers.identify([0]*48000)
    assert not vpid
    assert speakers.bind(track,"lux-id")
    reopened = LocalSpeakers(Extractor(),"model1",path,process.profiles)
    _, vpid, score = reopened.identify([0]*32000)
    assert vpid.startswith("local:model1:") and score == pytest.approx(1)
    ids = VoiceIdentities(process.profiles,"stone","session")
    assert ids.resolve("new",voiceprint_id=vpid,confidence=score).object_id == "lux-id"
    assert not LocalSpeakers(Extractor(),"other-model",path,process.profiles).known
    assert cosine([1,0],[0,1]) == 0


def test_ambiguous_voice_similarity_does_not_pick_a_person(tmp_path, process):
    speakers = LocalSpeakers(None,"m",tmp_path/"vectors.json",process.profiles)
    process.profiles.create(ObjectProfile("other","小明"))
    speakers.known={"a":{"object_id":"lux-id","embedding":[1,0]},"b":{"object_id":"other","embedding":[1,0]}}
    speakers.embedding=lambda samples: [1,0]
    assert speakers.identify([])[1] == ""


class FakeCloud:
    config = VoiceConfig("test")
    async def synthesize(self,text):
        yield b"\0\0" * 10


def test_streaming_synthesis_not_held_up_by_write_zone_and_stop_is_immediate(process):
    async def run():
        entered, release = Event(), Event()
        class Writer(Model):
            def generate(self, req):
                entered.set()
                assert release.wait(3)
                return ModelResponse(rewritten_context="准备回应",model="writer")
        process.write_zone = Writer()
        messages=[]
        async def send(payload): messages.append(payload)
        c=VoiceConversation(process,"stone",FakeCloud(),send)
        try:
            await c.accept(Transcript("你好","A",0,2000,True))
            await asyncio.wait_for(asyncio.to_thread(entered.wait),4)
            for _ in range(100):
                if any(m["type"]=="audio" for m in messages): break
                await asyncio.sleep(.005)
            assert any(m["type"]=="audio" for m in messages)
            assert c.delivery.snapshot()["played_text"] == ""
            rid=c.delivery.snapshot()["reply_id"]
            await c.accept(Transcript("停一下","A",2500,3500,True))
            await asyncio.wait_for(c.queue.join(),1)
            assert c.delivery.snapshot()["state"] == "stopped"
            assert not release.is_set()
            await c.acknowledge({"reply_id":rid,"segment":0,"phase":"completed"})
            assert c.delivery.snapshot()["played_text"] == ""
        finally:
            release.set()
            await c.close()
    asyncio.run(run())


def test_late_cognition_callback_after_stop_is_suppressed(process):
    async def run():
        entered,release=Event(),Event()
        class SlowModel(Model):
            def generate(self, req):
                entered.set(); assert release.wait(3)
                return super().generate(req)
        process.cognition=SlowModel()
        messages=[]
        async def send(payload): messages.append(payload)
        c=VoiceConversation(process,"stone",FakeCloud(),send)
        try:
            await c.accept(Transcript("你好","A",0,2000,True))
            await asyncio.wait_for(asyncio.to_thread(entered.wait),4)
            await c.accept(Transcript("别说了","A",2100,3200,True))
            await asyncio.wait_for(c.queue.join(),1)
            release.set()
            await asyncio.wait_for(c.turn_queue.join(),3)
            await asyncio.sleep(.01)
            assert not any(m["type"]=="audio" for m in messages)
        finally:
            release.set(); await c.close()
    asyncio.run(run())


def test_player_completion_enters_ledger_and_survives_reconnect(process):
    async def run():
        async def send(payload): pass
        c=VoiceConversation(process,"stone",FakeCloud(),send)
        d=c.delivery.begin("第一句。第二句。","lux-id")
        c.delivery.generated(d.reply_id)
        await c.acknowledge({"reply_id":d.reply_id,"segment":0,"phase":"completed"})
        await c.stop()
        await c.close()
        reconnected=VoiceConversation(process,"stone",FakeCloud(),send)
        try:
            assert "第一句。" in reconnected.delivery.context_note()
            assert "第二句。" in reconnected.delivery.context_note()
            segments=process.activity_ledger.list_experiences("stone")
            assert any(s.text_raw=="（播放器确认此句播放完成）第一句。" for s in segments)
        finally:
            await reconnected.close()
    asyncio.run(run())
