import asyncio
from types import SimpleNamespace
from jshi.voice.entry_scheduler import EntryScheduler
from jshi.voice.turn_schedule import TurnSchedule


def speech(i, text):
    return SimpleNamespace(input_id=i, text=text, start_ms=0, end_ms=1000, overlap=False,
        speaker=SimpleNamespace(object_id='person', track_id='track', status='recognized'))


def test_collision_runs_while_cognition_is_busy_without_entry_job():
    async def run():
        effects, calls = [], []
        class Service:
            config = {'mode': 'active', 'collision': True, 'collision_apply': True, 'threshold': .5}
            async def decide(self, purpose, state, revision):
                calls.append(state)
                return SimpleNamespace(status='ok', answers={'relation':'stop','old_work':'stop'},
                    choice=lambda key, threshold: {'relation':'stop','old_work':'stop'}[key])
        schedule = TurnSchedule()
        schedule.set_task('cognition', 'running')
        schedule.ack('reply', 0)
        scheduler = EntryScheduler(Service(), schedule=schedule)
        scheduler.work_busy = lambda: True
        scheduler.work_context = lambda: [speech('a', '解释一下'), speech('b', '不用了')]
        async def apply(effect): effects.append(effect)
        scheduler.on_effect = apply
        try:
            scheduler.notify([speech('b', '不用了')])
            await scheduler.work_task
            assert effects[0].kind == 'cancel'
            assert schedule.current().played == [('reply', 0, False)]
            assert [r['text'] for r in calls[0]['old']] == ['解释一下']
            assert schedule.current().inputs['b'] == '不用了'
        finally:
            await scheduler.close()
    asyncio.run(run())


def test_work_collision_correction_invalidates_only_unplayed_answer():
    schedule = TurnSchedule()
    schedule.set_task('cognition', 'running')
    schedule.add_answer('played', 1).played = True
    schedule.add_answer('pending', 1)
    schedule.note_text('b', '不是电脑，是手机')
    effect = schedule.apply_work_collision('replace', 'stop', schedule.capture())
    assert effect.kind == 'invalidate_work'
    assert schedule.current().answers['played'].eligible
    assert not schedule.current().answers['pending'].eligible


def test_record_only_work_input_does_not_block_ready_answer():
    class Service:
        config = {'mode': 'active', 'collision': True, 'collision_apply': False}
    schedule = TurnSchedule()
    schedule.set_task('cognition', 'running')
    scheduler = EntryScheduler(Service(), schedule=schedule)
    scheduler.notify([speech('fragment', '匠')])
    assert schedule.current().pending == []
    assert not schedule.blocks_start()


def test_record_only_input_during_entry_does_not_change_delivery_permission():
    async def run():
        from jshi.voice.entry_scheduler import EntryJob
        class Service:
            config = {'mode': 'active', 'collision': True, 'collision_apply': False, 'threshold': .85}
            async def decide(self, *args):
                return SimpleNamespace(status='ok', answers={}, provider='test', model='test',
                    elapsed_ms=0, server_ms=0, reason='', choice=lambda *args: 'uncertain')
        schedule = TurnSchedule()
        schedule.begin_attempt(('question',))
        scheduler = EntryScheduler(Service(), schedule=schedule)
        scheduler.current = EntryJob((speech('question', '你会做什么动作'),))
        try:
            before = schedule.capture()
            scheduler.notify([speech('tail', '啥')])
            await scheduler.task
            assert schedule.capture() == before
            assert not schedule.blocks_start()
        finally:
            await scheduler.close()
    asyncio.run(run())
