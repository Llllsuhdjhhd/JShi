import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

from jshi.voice.cloud_alignment import complete_alignment


def test_slow_split_is_retained_and_never_delivered_as_whole_span():
    async def run():
        release = Event()
        notified = asyncio.Event()
        expected = ('speaker-a', 'speaker-b')
        def process(row):
            assert release.wait(3)
            return expected
        async def slow():
            notified.set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = asyncio.create_task(complete_alignment(SimpleNamespace(process=process),
                object(), executor, .01, slow))
            try:
                await asyncio.wait_for(notified.wait(), 1)
                assert not task.done()
            finally:
                release.set()
            assert await task == expected
    asyncio.run(run())


def test_session_cancel_does_not_deliver_late_split():
    async def run():
        release = Event()
        notified = asyncio.Event()
        def process(row):
            release.wait(3)
            return ('late',)
        async def slow():
            notified.set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = asyncio.create_task(complete_alignment(SimpleNamespace(process=process),
                object(), executor, .01, slow))
            try:
                await asyncio.wait_for(notified.wait(), 1)
                task.cancel()
                try:
                    await task
                    assert False, 'cancelled session must not deliver'
                except asyncio.CancelledError:
                    pass
            finally:
                release.set()
    asyncio.run(run())
