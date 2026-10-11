import asyncio

from jshi.app.voice import VoiceConversation
from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence
from jshi.models import ModelResponse, ResponseItem, ResponsePlan
from tests.test_voice import FakeCloud, Model, process


def test_silent_action_is_emitted_without_audio_and_with_real_phases(process):
    class Silent(Model):
        def generate(self, request):
            self.requests.append(request)
            return ModelResponse(model='test', response_plan=ResponsePlan('ignore',
                items=(ResponseItem('embodied', '转向声音来处'),)))
    process.cognition=Silent()
    async def run():
        emitted=[]
        async def send(m): emitted.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            await c.typed('你好','lux-id')
            await asyncio.wait_for(c.turn_queue.join(),3)
            await asyncio.sleep(.02)
            assert next(m for m in emitted if m['type']=='embodied')['actions']==['转向声音来处']
            assert not any(m['type'] in {'reply','audio'} for m in emitted)
            assert [m['stage'] for m in emitted if m['type']=='flow_phase' and m.get('lane')=='main']==[
                'resolve','assemble','cognition','action']
        finally: await c.close()
    asyncio.run(run())


def test_visual_backlog_retains_only_latest_and_does_not_merge_into_speech(process):
    async def run():
        async def send(m): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send,environment_idle_s=.1)
        # Hold both consumers so queue contents can be examined deterministically.
        c.turn_worker.cancel()
        await asyncio.gather(c.turn_worker,return_exceptions=True)
        try:
            c.input_idle.clear()
            who=SpeakerEvidence('visual',method='visual_scene')
            def scene(text): return InputEnvelope('test','visual_environment',(InputPart('text',text),),who)
            c.enqueue_turn(scene('旧画面'))
            c.enqueue_turn(scene('最新画面'))
            voice=SpeakerEvidence('typed','lux-id','lux','recognized','user_selected')
            c.enqueue_turn(InputEnvelope('test','web_text',(InputPart('text','我的发言'),),voice))
            await asyncio.sleep(.12)
            speech,_=c.turn_queue.get_nowait();c.turn_queue.task_done()
            assert speech.source=='web_text' and speech.text=='我的发言'
            assert c.pending_environment.text=='最新画面'
            assert c.turn_queue.empty()
            c.input_idle.set()
            await asyncio.sleep(.12)
            visual,_=c.turn_queue.get_nowait();c.turn_queue.task_done()
            assert visual.text=='最新画面'
        finally: await c.close()
    asyncio.run(run())
