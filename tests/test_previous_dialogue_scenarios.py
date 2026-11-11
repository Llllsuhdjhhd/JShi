"""Previously reported scenarios: verify plumbing, not ASR/JEV model accuracy."""
import asyncio
from types import SimpleNamespace
from jshi.voice.contracts import Transcript, SpeechSegment
from jshi.voice.utterance_organizer import UtteranceOrganizer
from jshi.voice.transcription_recovery import RecoveryIntake
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.entry_scheduler import EntryScheduler
from jshi.voice.turn_schedule import TurnSchedule
from tests.test_work_collision import speech


def test_disfluent_shopping_expression_keeps_all_three_parts():
    organizer = UtteranceOrganizer()
    for text,start,end in [('我要去一下',0,1000),('商店去买',3000,3500),
                            ('鸡蛋也要买几个，冰箱好像没了',6000,7500)]:
        organizer.observe([Transcript(text,'A',start,end,True,voiceprint_id='A')])
    assert len(organizer.records) == 1
    assert organizer.material()[0]['text'] == '我要去一下 / 商店去买 / 鸡蛋也要买几个，冰箱好像没了'
    assert organizer.material()[0]['state'] == 'unknown'


def test_long_speech_recovers_once_at_pause_not_at_elapsed_checkpoint():
    async def run():
        audio = SessionAudio();audio.append(b'\0\0'*128000)
        got = asyncio.Event()
        calls = []
        async def recognize(pcm,start):
            calls.append(start)
            return (Transcript('这个问题我还没说完','A',0,8000,True),)
        async def deliver(rows,**kw):got.set()
        recovery=RecoveryIntake(audio,recognize,deliver,lambda *a:None)
        try:
            await recovery.offer([SpeechSegment([], 'A',0,8000,speaker_cluster_id='A')],0,8000)
            await asyncio.sleep(0)
            assert not calls and not got.is_set()
            # Elapsed time alone must not turn an unfinished utterance into a
            # second ASR input. The existing pause boundary releases recovery.
            await recovery.offer([SpeechSegment([], 'A',0,8000,speaker_cluster_id='A')],0,8300)
            await asyncio.wait_for(got.wait(),1)
            await recovery.offer([SpeechSegment([], 'A',0,8000,speaker_cluster_id='A')],0,8400)
            await asyncio.sleep(0)
            assert calls == [0]
        finally:await recovery.close()
    asyncio.run(run())


def test_continuous_collision_is_bounded_and_all_original_inputs_survive():
    async def run():
        schedule=TurnSchedule();schedule.set_task('cognition','running')
        effects=[]
        class Service:
            config={'mode':'active','collision':True,'collision_apply':True,'threshold':.5}
            calls=0
            async def decide(self,*args):
                self.calls+=1
                scheduler.notify([speech(str(self.calls),f'补充{self.calls}')])
                return SimpleNamespace(status='ok',answers={},choice=lambda *a:None)
        service=Service();scheduler=EntryScheduler(service,schedule=schedule)
        scheduler.work_busy=lambda:True
        async def effect(e):effects.append(e)
        scheduler.on_effect=effect
        try:
            scheduler.notify([speech('initial','还有一点')])
            await asyncio.wait_for(scheduler.work_task,1)
            # Speech kept changing: the old cognition stays valid instead of pausing playback.
            assert service.calls==3 and effects[-1].kind=='reapprove'
            assert set(effects[-1].covered)=={'initial','1','2','3'}
            assert set(schedule.current().inputs)=={'initial','1','2','3'}
        finally:await scheduler.close()
    asyncio.run(run())
