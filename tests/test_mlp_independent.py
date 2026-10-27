import asyncio
from concurrent.futures import Future

from jshi.app.voice import VoiceConversation
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.jev import VoiceJEV
from jshi.voice.volc import Transcript
from jshi.core.envelope import SpeakerEvidence
from tests.test_voice import FakeCloud,process
from tests.test_voice_control import ScriptedControl


def test_mlp_runs_during_stop_without_delaying_control_and_publishes_late_scores(process):
    class Models:
        calls=0
        future=Future()
        def collect_original(self,*args,**kwargs): return None
        def submit_predict(self,pcm,speakers):
            assert pcm
            self.calls+=1
            return self.future
    async def run():
        events=[]
        async def send(m):events.append(m)
        audio=SessionAudio();audio.append(b'\x01\x00'*16000)
        models=Models()
        c=VoiceConversation(process,'stone',FakeCloud(),send,sample_source=audio,
            personal_speakers=models,jev=VoiceJEV(ScriptedControl('stop'), mode='legacy'))
        c.person_reviewer=None
        try:
            c.active_turn={'state':'thinking'}
            await c.accept(Transcript('先停下', 'unidentified-a',0,1000,True))
            await asyncio.wait_for(c.queue.join(),1)
            assert any(m['type']=='stop' for m in events), [m for m in events if m['type']=='notice']
            assert models.calls==1 and not models.future.done()
            iid=next(iter(c.input_records))
            assert c.input_records[iid]['personal_classifier']['status']=='pending'
            models.future.set_result({'status':'uncertain','matched':'','ranking':[
                {'object_id':'lux-id','label':'lux','score':.999,'verified':False},
                {'object_id':'other-id','label':'other','score':.001,'verified':False}]})
            await asyncio.wait_for(c.personal_prediction_jobs[iid],1)
            update=next(m for m in events if m['type']=='personal_prediction')
            assert update['input_id']==iid and len(update['personal_prediction']['ranking'])==2
            assert not c.candidate_cache.get(iid)
            assert update['personal_prediction']['ranking'][0]['score']==.999
            assert update['identity_display']['label']=='lux' and update['identity_display']['state']=='可能'
        finally:await c.close()
    asyncio.run(run())


def test_multiple_inputs_queue_independently_instead_of_skipping_when_busy(process):
    class Models:
        def __init__(self): self.futures=[Future(),Future()];self.calls=0
        def submit_predict(self,pcm,speakers):
            result=self.futures[self.calls];self.calls+=1;return result
    async def run():
        async def send(m):pass
        models=Models();c=VoiceConversation(process,'stone',FakeCloud(),send,personal_speakers=models)
        try:
            for i in range(2):
                t=Transcript('输入'+str(i),'unknown',0,1000,True,input_id=str(i))
                c.input_records[str(i)]={'pcm':b'\0\0'*16000,'transcript':t}
                await c.personal_identity(t,SpeakerEvidence('unknown'),wait=False)
            await asyncio.sleep(0)
            assert models.calls==1
            for i,score in enumerate((.7,.3)):
                models.futures[i].set_result({'status':'uncertain','matched':'','ranking':[
                    {'object_id':'lux-id','label':'lux','score':score}]})
                await c.personal_prediction_jobs[str(i)]
                await asyncio.sleep(0)
                assert c.input_records[str(i)]['personal_classifier']['ranking'][0]['score']==score
            assert models.calls==2
        finally:await c.close()
    asyncio.run(run())
