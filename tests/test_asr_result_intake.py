import asyncio
import pytest
from jshi.voice.streaming_asr import buffered_results


def test_provider_diagnostics_preserve_original_repetition_and_sent_audio():
    from hashlib import sha256
    from types import SimpleNamespace
    from aiohttp import WSMsgType
    from jshi.voice.streaming_asr import StreamingASRSession
    from jshi.voice.contracts import Transcript
    async def run():
        raw = {'result': {'text': '匠石。匠石。'}}
        row = Transcript(text='匠石。匠石。', track_id='1', start_ms=0, end_ms=800, final=True)
        class WS:
            async def __aiter__(self):
                yield SimpleNamespace(type=WSMsgType.BINARY)
        class Protocol:
            assembler = SimpleNamespace(accept=lambda payload: [row])
            async def send(self, ws, pcm):
                if pcm == b'fail':
                    raise RuntimeError('send failed')
            def decode(self, message): return raw, True
        session = StreamingASRSession(WS(), Protocol())
        records = []
        session.report = lambda event, data: records.append((event, data))
        await session.send_audio(b'original')
        with pytest.raises(RuntimeError): await session.send_audio(b'fail')
        results = [r async for r in session.results()]
        assert results[0][0][0].text == '匠石。匠石。'
        record = records[0][1]
        assert record['payload'] == raw
        assert record['sent_bytes'] == 8
        assert record['sent_sha256'] == sha256(b'original').hexdigest()
    asyncio.run(run())


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
