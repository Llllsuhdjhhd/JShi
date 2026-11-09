import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import time

import pytest

from jshi.voice.cloud_alignment import complete_alignment
from jshi.voice.timing import phase, stage, input_stage
from jshi.voice.streaming_asr import buffered_results


def test_alignment_trace_separates_executor_backlog_and_computation():
    async def run():
        release = Event()
        events = []
        row = SimpleNamespace(start_ms=1, end_ms=20001, words=())
        def process(row):
            with phase('segmentation_model_ms'):
                time.sleep(.015)
            return ('split-a', 'split-b')
        async def slow():
            release.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocker = pool.submit(release.wait, 2)
            result = await complete_alignment(SimpleNamespace(process=process), row, pool,
                .02, slow, report=lambda kind,data: events.append((kind,data)))
            blocker.result()
        assert result == ('split-a', 'split-b')
        traced = [d for k,d in events if k == 'cloud_alignment_timing']
        worker = next(d for d in traced if d['state'] == 'worker_finished')
        assert worker['executor_queue_ms'] >= 10
        assert worker['worker_ms'] >= worker['phases_ms']['segmentation_model_ms'] >= 10
        assert len({d['alignment_id'] for d in traced}) == 1
        assert traced[-1]['state'] == 'wait_finished'
        assert traced[-1]['worker_done']
    asyncio.run(run())


def test_worker_completion_is_logged_even_when_waiter_is_cancelled():
    async def run():
        release = Event()
        notified = asyncio.Event()
        events = []
        def process(row):
            release.wait(2)
            return ('late',)
        async def slow(): notified.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = asyncio.create_task(complete_alignment(SimpleNamespace(process=process),
                object(), pool, .001, slow, report=lambda k,d: events.append((k,d))))
            await notified.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            release.set()
        states = [d['state'] for k,d in events if k == 'cloud_alignment_timing']
        assert 'wait_finished' in states and 'worker_finished' in states
        assert next(d for k,d in events if d.get('state') == 'wait_finished')['status'] == 'CancelledError'
    asyncio.run(run())


def test_late_loop_resume_is_not_counted_as_worker_time():
    async def run():
        release = Event()
        finished = Event()
        events = []
        def process(row):
            release.wait(2)
            return ('ok',)
        def report(kind, data):
            events.append((kind,data))
            if data.get('state') == 'worker_finished': finished.set()
        async def slow():
            release.set()
            # Deliberately stall the loop after the worker has finished.
            assert finished.wait(2)
            time.sleep(.03)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert await complete_alignment(SimpleNamespace(process=process), object(), pool,
                .001, slow, report=report) == ('ok',)
        end = next(d for k,d in events if d.get('state') == 'wait_finished')
        assert end['loop_resume_ms'] >= 20
    asyncio.run(run())


def test_asr_buffer_records_arrival_before_slow_consumer():
    async def run():
        events = []
        row = SimpleNamespace(start_ms=0, end_ms=500, final=True)
        class Session:
            async def results(self):
                yield (row,), False
                yield (row,), True
        stream = buffered_results(Session(), report=lambda k,d:events.append(d))
        await anext(stream)
        await asyncio.sleep(.02)
        await anext(stream)
        await stream.aclose()
        consumed = [d for d in events if d['state'] == 'consumed']
        assert consumed[1]['buffer_wait_ms'] >= 10
        assert consumed[1]['received_at_ms'] == [d for d in events if d['state']=='received'][1]['received_at_ms']
    asyncio.run(run())


def test_trace_failures_do_not_change_result_and_keyword_calls_still_work():
    def broken(*args): raise RuntimeError('log failed')
    class Owner:
        record = staticmethod(broken)
        @input_stage('test')
        async def operation(self, transcript, *, wait=True):
            return transcript
    async def run():
        row = SimpleNamespace(input_id='I1')
        assert await Owner().operation(transcript=row, wait=False) is row
        with stage(broken, 'failed-operation'):
            pass
    asyncio.run(run())
