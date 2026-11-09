import asyncio
import pytest
from jshi.voice.streaming_asr import buffered_results


def test_slow_consumer_does_not_block_cloud_read_and_arrival_clock():
    async def run():
        drained=asyncio.Event()
        class Session:
            async def results(self):
                yield ('first',),False
                yield ('second',),True
                drained.set()
        stream=buffered_results(Session())
        first=await anext(stream)
        # The pump reaches final independently of this consumer.
        await asyncio.sleep(0)
        second=await anext(stream)
        assert first[0]==('first',) and second[0]==('second',)
        assert second[2]>=first[2]
        await stream.aclose()
    asyncio.run(run())


def test_upstream_failure_propagates_after_queued_results():
    async def run():
        class Session:
            async def results(self):
                yield ('first',),False
                raise RuntimeError('disconnected')
        stream=buffered_results(Session())
        assert (await anext(stream))[0]==('first',)
        with pytest.raises(RuntimeError,match='disconnected'):
            await anext(stream)
    asyncio.run(run())


def test_intake_overflow_is_explicit_and_memory_bounded():
    async def run():
        class Session:
            async def results(self):
                for n in range(10):yield (n,),False
        stream=buffered_results(Session(),capacity=2)
        assert (await anext(stream))[0]==(0,)
        assert (await anext(stream))[0]==(1,)
        with pytest.raises(RuntimeError):await anext(stream)
    asyncio.run(run())
