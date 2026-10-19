import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from jshi.voice.local_input import LocalAudioPump


def test_receiving_and_controls_do_not_wait_for_local_decode():
    async def run():
        entered, release = Event(), Event()
        calls, accepted = [], []
        class Decoder:
            def feed(self, pcm, final=False):
                calls.append((pcm, final))
                if len(calls) == 1:
                    entered.set()
                    assert release.wait(2)
                return [pcm] if pcm else []
        async def accept(value):
            accepted.append(value)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pump = LocalAudioPump(Decoder(), executor, accept)
            try:
                pump.feed(b'aa')
                assert await asyncio.to_thread(entered.wait, 1)
                # WebSocket receiver can keep receiving PCM and process stop
                # while the single decoder remains blocked.
                pump.feed(b'bb')
                pump.feed(b'cc')
                control = asyncio.Event()
                asyncio.get_running_loop().call_soon(control.set)
                await asyncio.wait_for(control.wait(), .2)
                assert accepted == []
            finally:
                release.set()
                await pump.finish()
            assert b''.join(accepted) == b'aabbcc'
            assert calls == [(b'aa', False), (b'bbcc', False), (b'', True)]
    asyncio.run(run())


def test_local_pump_chunks_overload_and_failure_are_explicit():
    async def run():
        calls = []
        class Decoder:
            def feed(self, pcm, final=False):
                calls.append(pcm)
                return []
        async def accept(value): pass
        with ThreadPoolExecutor(max_workers=1) as executor:
            pump = LocalAudioPump(Decoder(), executor, accept)
            pump.feed(b'aa' * 20000)
            with pytest.raises(BufferError):
                pump.feed(b'bb' * 600000)
            await pump.finish()
            assert b''.join(calls) == b'aa' * 20000
            assert max(map(len, calls)) <= 16000
            class Broken:
                def feed(self, pcm, final=False):
                    raise ValueError('decoder failed')
            pump = LocalAudioPump(Broken(), executor, accept)
            pump.feed(b'aa')
            with pytest.raises(ValueError, match='decoder failed'):
                await pump.finish()
            with pytest.raises(ValueError, match='decoder failed'):
                pump.feed(b'bb')
    asyncio.run(run())
