import asyncio
import json

from jshi.app.voice import VoiceConversation
from jshi.voice.config import ASR_HOTWORDS, VoiceConfig
from jshi.voice.volc import Transcript, TranscriptAssembler, VolcVoice
from tests.test_voice import FakeCloud, process


def payload(text, start=100, end=2000, final=True, speaker='0'):
    return {'result': {'utterances': [{'text': text, 'start_time': start,
        'end_time': end, 'definite': final, 'speaker_id': speaker}]}}


def test_final_text_revision_replaces_audio_without_repeating_or_rolling_back():
    a = TranscriptAssembler()
    assert not a.accept(payload('声文', final=False))[0].final
    assert a.accept(payload('声文'))[0].replaces == ()
    assert a.accept(payload('声纹'))[0].replaces == ((100, 2000),)
    assert not a.accept(payload('声文'))  # repeated old history cannot undo correction
    assert not a.accept(payload('声纹', speaker='1'))  # label-only revision
    assert not a.accept(payload('声纹', final=False))  # partial cannot replace final


def test_regrouped_history_cannot_merge_different_people_into_one_input():
    a = TranscriptAssembler()
    a.accept(payload('我说声', end=1000))
    a.accept(payload('文识别', start=1000, speaker='1'))
    revision = a.accept(payload('我说声纹识别'))
    assert not revision


def test_hotwords_serialized_inside_real_asr_first_packet(monkeypatch):
    monkeypatch.delenv('JSHI_VOICE_END_WINDOW_MS', raising=False)
    monkeypatch.setenv('JSHI_VOICE_ASR_HOTWORDS', '匠石，MLP,声纹;匠石')
    config = VoiceConfig.from_env(asr='local', tts='local')
    req = VolcVoice(config,None).asr_request()['request']
    assert req['end_window_size'] == 800 and req['enable_nonstream']
    assert json.loads(req['corpus']['context']) == {'hotwords': [
        {'word':'匠石'}, {'word':'MLP'}, {'word':'声纹'}]}
    monkeypatch.setenv('JSHI_VOICE_ASR_HOTWORDS', '')
    assert 'corpus' not in VolcVoice(VoiceConfig.from_env(asr='local',tts='local'),None).asr_request()['request']
    assert 'JEV' in ASR_HOTWORDS


def test_revision_updates_original_input_and_does_not_queue_second_turn(process):
    async def run():
        sent=[]
        async def send(m): sent.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            await c.accept(Transcript('声文识别','0',100,2000,True))
            iid=next(iter(c.input_records))
            c.dialogue.input('stone',iid,'声文识别','lux')
            await c.accept(Transcript('声纹识别','1',100,2000,True,replaces=((100,2000),)))
            assert c.queue.qsize()==1 and len(c.input_records)==1
            updated=c.input_records[iid]['transcript']
            assert updated.text=='声纹识别' and updated.track_id=='0'
            assert '声纹识别' in c.dialogue.note('stone')
            assert '声文识别' not in c.dialogue.note('stone')
            event=next(m for m in sent if m['type']=='transcript_revision')
            assert event['input_ids']==[iid]
            c.input_records[iid]['processing_started']=True
            await c.accept(Transcript('声纹识别模型','1',100,2000,True,replaces=((100,2000),)))
            assert c.queue.qsize()==1
            assert any('声纹识别模型' in e['payload']['text']
                for e in c.jev_scene_store.snapshot()['events'])
        finally:
            await c.close()
    asyncio.run(run())
