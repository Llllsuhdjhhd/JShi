import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest

from jshi.app.voice import VoiceConversation, bounded_online_match
from jshi.voice.jev import VoiceJEV
from jshi.voice.volc import Transcript
from tests.test_voice import FakeCloud, process
from tests.test_voice_control import ScriptedControl


@pytest.mark.parametrize('text',['别说啦','好了好了别说啦','好了好了，别说了','匠石，别说啦',
                               '别说了别说了','先先不要说了不要说了'])
def test_colloquial_stop_uses_the_semantic_model(text):
    model=ScriptedControl('stop')
    assert VoiceJEV(model).decide_batch('s',[{'n':1,'text':text}],[],{}).control=='stop'
    assert len(model.requests)==1


def test_quoted_stop_is_not_a_stop_command():
    assert VoiceJEV().decide('s','他说别说啦这句话',{},{}).action!='stop'


def test_stop_bypasses_busy_input_and_invalidates_old_generation(process):
    async def run():
        sent=[]
        async def send(message):sent.append(message)
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(ScriptedControl('stop')))
        c.person_reviewer=None
        try:
            old_epoch=c.epoch
            c.active_turn={'state':'thinking'}
            c.input_pause_s=12
            await c.accept(Transcript('好了好了别说啦','A',0,1800,True))
            await asyncio.wait_for(c.queue.join(),1)
            assert c.epoch==old_epoch+1
            assert c.queue.empty() and c.turn_queue.empty()
            assert any(m['type']=='stop' for m in sent)
            await c._start_reply('旧回答不应复活','lux-id',old_epoch)
            assert not any(m['type']=='reply' for m in sent)
        finally:await c.close()
    asyncio.run(run())


def test_slow_online_identity_is_bounded_and_does_not_accumulate():
    release=Event()
    class Audio:
        calls=0
        def match(self,transcript,speakers):
            self.calls+=1
            assert release.wait(3)
            return transcript
    async def run():
        audio=Audio()
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                t=Transcript('你好','A',0,1000,True)
                result,pending=await asyncio.wait_for(bounded_online_match(audio,t,object(),executor,None,.02),.5)
                assert result.identity_uncertain and not pending.done()
                next_result,same=await bounded_online_match(audio,t,object(),executor,pending,.02)
                assert same is pending and next_result.identity_uncertain
                assert audio.calls==1
            finally:release.set()
            await pending
    asyncio.run(run())


def test_stop_button_path_discards_queued_input(process):
    async def run():
        sent=[]
        async def send(message):sent.append(message)
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            # No await between enqueue and stop: this is still unprocessed input.
            c.queue.put_nowait(Transcript('旧问题','A',0,1000,True))
            await c.stop()
            assert c.queue.empty()
            await asyncio.wait_for(c.queue.join(), .5)
            assert any(m['type']=='stop' for m in sent)
        finally:await c.close()
    asyncio.run(run())
