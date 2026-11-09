import asyncio
from tests.test_voice import process, FakeCloud
from jshi.app.voice import VoiceConversation
from jshi.models import ResponseItem


def test_blocked_reply_is_released_without_playback_callback(process):
    async def run():
        messages=[]
        async def send(m): messages.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            s=c.entry_scheduler.schedule
            s.speech_hold=True
            await c.apply_schedule_effect(s.short_voice())
            await c._start_plan((ResponseItem('verbal','你好',()),),'lux-id',c.epoch,(c.epoch,1))
            assert not any(m['type']=='reply' for m in messages)
            await c.apply_schedule_effect(s.confirm_transient(s.note_short_end()))
            assert any(m['type']=='reply' for m in messages)
            assert s.current().answers
        finally: await c.close()
    asyncio.run(run())


def test_main_work_registers_pending_text_without_entry_running(process):
    async def run():
        from types import SimpleNamespace
        async def send(m): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            c.decisions.config['mode']='active'
            c.decisions.config['collision_apply']=True
            s=c.entry_scheduler.schedule
            s.set_task('cognition','running')
            c.entry_scheduler.notify((SimpleNamespace(input_id='new',text='不是电脑，是手机'),))
            assert s.current().pending == ['new']
            assert s.blocks_start()
        finally: await c.close()
    asyncio.run(run())


def test_reapproval_carries_removed_pause_reason_and_monotonic_messages(process):
    async def run():
        messages=[]
        async def send(m): messages.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            s=c.entry_scheduler.schedule
            s.speech_hold=True
            s.begin_attempt(('one',))
            await c._start_plan((ResponseItem('verbal','你好',()),),'lux-id',c.epoch,(c.epoch,1))
            pause=s.sustained_voice()
            await c.apply_schedule_effect(pause)
            s.note_text('two','嗯，继续')
            effect=s.apply_collision('independent','continue',('two',),s.capture())
            await c.apply_schedule_effect(effect)
            resumed=[m for m in messages if m['type']=='resume'][-1]
            assert pause.reason in resumed['cleared']
            previous=resumed['permit_seq']
            s.open_matter()
            await c.stop()
            assert [m for m in messages if m['type']=='stop'][-1]['permit_seq'] > previous
        finally: await c.close()
    asyncio.run(run())
