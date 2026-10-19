import asyncio
from dataclasses import asdict
import hashlib
import io
import wave

from aiohttp.test_utils import TestClient, TestServer

from jshi.app.voice import VoiceConversation,create_app
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.config import VoiceConfig
from jshi.voice.recent_audio import RecentAudio
from jshi.voice.volc import Transcript
from tests.test_voice import FakeCloud,process


def pcm(n=16000):
    return b'\x01\x02'*n


def raw_wav(value):
    with wave.open(io.BytesIO(value),'rb') as f:
        assert (f.getframerate(),f.getnchannels(),f.getsampwidth())==(16000,1,2)
        return f.readframes(f.getnframes())


def test_restart_retains_exact_input_even_after_ram_is_trimmed(tmp_path):
    archive=RecentAudio(tmp_path)
    source=SessionAudio(seconds=1,archive=archive)
    original=pcm(48000)
    source.append(original)
    t=Transcript('真的吗','t',500,1500,True,input_id='one')
    part=source.segment(500,1500)
    assert part==original[16000:48000]  # old start is outside the RAM window
    archive.save_input('one',source.capture_id,asdict(t),pcm=part)
    archive.update('one',classifier={'ranking':[{'label':'lux','score':.042,'version':'v1'}]},
                   features={'model_id':'encoder','vectors':[[1.,0.]],'durations':[1.]})
    reopened=RecentAudio(tmp_path)
    assert raw_wav(reopened.wav('one'))==part
    assert raw_wav(reopened.wav('one',padding_ms=500))==original[:64000]
    row=reopened.get('one')
    assert row['metadata']['classification_pcm_sha256']==hashlib.sha256(part).hexdigest()
    assert row['metadata']['features']['vectors']==[[1.,0.]]


def test_connection_capture_ids_do_not_mix_even_if_timestamps_repeat(tmp_path):
    archive=RecentAudio(tmp_path)
    a,b=SessionAudio(archive=archive),SessionAudio(archive=archive)
    a.append(pcm());b.append(b'\x03\x04'*16000)
    assert a.capture_id!=b.capture_id
    assert archive.segment(a.capture_id,0,1000)==pcm()
    assert archive.segment(b.capture_id,0,1000)==b'\x03\x04'*16000


def test_capacity_and_age_clean_audio_and_mark_removed_clips_unavailable(tmp_path):
    archive=RecentAudio(tmp_path,max_bytes=32000)
    archive.chunk_bytes=32000
    archive.append('capture',0,pcm(32000))
    archive.save_input('old','capture',asdict(Transcript('旧句','t',0,1000,True)))
    archive.prune()
    assert archive.wav('old') is None
    assert archive.segment('capture',1000,2000)==pcm()
    assert archive.recent()[0]['audio_available'] is False
    with archive._db() as db:
        db.execute('UPDATE chunks SET updated=0');db.execute('UPDATE inputs SET created=0')
    archive.prune()
    assert not archive.recent() and not list(tmp_path.glob('*.pcm'))


def test_disk_failure_does_not_break_live_memory_audio(tmp_path,monkeypatch):
    archive=RecentAudio(tmp_path)
    def fail(*a):raise OSError('disk unavailable')
    monkeypatch.setattr(archive,'append',fail)
    source=SessionAudio(archive=archive)
    source.append(pcm())
    assert source.archive_error and source.segment(0,1000)==pcm()


def test_stop_close_and_new_app_keep_audio_available_in_api(process):
    async def run():
        root=process.repository.path.parent/'recent_audio'/hashlib.sha256(b'stone').hexdigest()[:16]
        archive=RecentAudio(root);source=SessionAudio(archive=archive);source.append(pcm(48000))
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send,sample_source=source)
        await c.accept(Transcript('真的吗','t',500,1500,True))
        iid=next(iter(c.input_records))
        await c.stop();await c.close()
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig('')))) as client:
            listing=await (await client.get('/api/recent-audio')).json()
            assert listing['inputs'][0]['input_id']==iid and listing['inputs'][0]['audio_available']
            part=listing['recordings'][0]
            stream=await client.get('/api/recent-audio-stream/'+part['capture']+'/'+str(part['start'])+'.wav')
            assert raw_wav(await stream.read())==pcm(48000)
            response=await client.get('/api/recent-audio/'+iid+'.wav')
            assert response.status==200 and raw_wav(await response.read())==pcm()
            response=await client.get('/api/recent-audio/'+iid+'.wav?padding_ms=500')
            assert len(raw_wav(await response.read()))==64000
            assert (await client.get('/api/recent-audio/'+iid+'.wav?padding_ms=9999')).status==400
            assert (await client.get('/api/recent-audio/not-found.wav')).status==404
            assert (await client.get('/recent-audio')).status==200
    asyncio.run(run())


def test_actual_classifier_features_and_version_are_saved_with_the_same_pcm(process,tmp_path):
    from tests.test_personal_speakers import speakers,publish
    from jshi.voice.personal import PersonalSpeakers
    engine=PersonalSpeakers(tmp_path/'models',process.profiles)
    engine.select_targets(['lux-id'])
    version=publish(engine,'lux-id',tmp_path,4)['version']
    archive=RecentAudio(tmp_path/'audio');source=SessionAudio(archive=archive);source.append(pcm())
    async def run():
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send,sample_source=source,
            local_speakers=speakers(),personal_speakers=engine,input_pause_s=.01)
        try:
            await c.accept(Transcript('真的吗','unidentified-archive',0,1000,True))
            await asyncio.wait_for(c.queue.join(),3)
            row=archive.recent()[0];m=row['metadata']
            assert m['classifier']['ranking'][0]['version']==version
            assert m['features']['model_id']=='fixture-encoder'
            assert m['features']['vectors']==[[1.,0.]] and m['features']['durations']==[1.]
            assert m['classification_pcm_sha256']==hashlib.sha256(raw_wav(archive.wav(row['input_id']))).hexdigest()
        finally:await c.close()
    try:asyncio.run(run())
    finally:engine.close()
