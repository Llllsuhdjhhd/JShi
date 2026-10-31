"""Time-driven replay with explicit real adapters, never implicit fake results."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
from time import monotonic
from uuid import uuid4
import wave

from .store import write_json


async def replay(store, case, runs_root, *, decoder_factory=None, gate_factory=None,
                 modes=('combined', 'split', 'decision'), realtime=True, observation_ms=1000):
    store.validate(case)
    if any(e['type'] == 'audio' for e in case['timeline']) and decoder_factory is None:
        raise ValueError('音频回放需要明确提供真实decoder_factory；不会用预先转写替代')
    if any(e['type'] == 'jev' for e in case['timeline']) and gate_factory is None:
        raise ValueError('JEV回放需要gate_factory，模型配置缺失')
    if not 0 <= observation_ms <= 30000:
        raise ValueError('观察期须在0至30000毫秒')
    run = Path(runs_root).resolve() / uuid4().hex
    run.mkdir(parents=True)
    write_json(run / 'scenario.json', case)
    origin = monotonic()
    events, results, requests = [], [], []
    def record(kind, **values):
        events.append({'type': kind, 'elapsed_ms': (monotonic()-origin)*1000, **values})
    async def wait(at):
        if realtime:
            await asyncio.sleep(max(0, origin + at/1000 - monotonic()))
    # Media integrity is checked before sending any input.
    schedule = []
    for index, event in enumerate(case['timeline']):
        if event['type'] == 'jev':
            schedule.append((event['at_ms'], index, 'jev', event))
            continue
        folder = store.path('media', event['media'])
        meta = json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
        raw = (folder/'original.wav').read_bytes()
        if hashlib.sha256(raw).hexdigest() != meta['sha256']:
            raise ValueError('录音校验失败，素材已变化')
        with wave.open(str(folder/'original.wav'), 'rb') as audio:
            pcm = audio.readframes(audio.getnframes())
        receipts = meta['receipts']
        if receipts is None:
            receipts = [{'start_sample': n, 'samples': min(1600, meta['frames']-n),
                         'received_ms': min(n+1600, meta['frames'])/16}
                        for n in range(0, meta['frames'], 1600)]
        for receipt in receipts:
            schedule.append((event['at_ms']+receipt['received_ms'], index, 'audio',
                (receipt, pcm[receipt['start_sample']*2:(receipt['start_sample']+receipt['samples'])*2], meta['timing_basis'])))
    decoder = None
    executor = ThreadPoolExecutor(max_workers=1)
    pending = []
    gates = {}
    locks = {mode: asyncio.Lock() for mode in modes}
    pumps = None
    status, error = 'completed', None
    try:
        if decoder_factory:
            (run/'state').mkdir(parents=True, exist_ok=True)
            decoder = decoder_factory(run/'state')
            from jshi.voice.local_input import LocalAudioPump
            async def accept(batch):
                record('transcripts_ready', items=[asdict(t) for t in batch])
            pumps = LocalAudioPump(decoder, executor, None, accept_batch=accept,
                on_timings=lambda ts, timings: record('pipeline_timings', timings=timings))
        # Loading local models can take seconds. Start the playback clock only
        # after initialization, otherwise all past-due packets arrive in a burst.
        origin = monotonic()
        async def judge_once(index, event, mode):
            state = run/'state'/mode
            state.mkdir(parents=True, exist_ok=True)
            if mode not in gates:
                gates[mode] = gate_factory(mode, state)
            gate = gates[mode]
            delivery = {**deepcopy(event.get('delivery', {})), **getattr(gate, 'replay_delivery', {})}
            def on_request(req):
                from jshi.models.prompt import build_system, build_user
                requests.append({'event': index, 'mode': mode, 'system': build_system(req), 'user': build_user(req)})
                record('jev_request', event=index, mode=mode)
            record('jev_start', event=index, mode=mode)
            try:
                result = await asyncio.to_thread(gate.decide_batch, 'entry-test', deepcopy(event['batch']),
                    deepcopy(event.get('context', [])), delivery, on_request=on_request)
                result = asdict(result) if is_dataclass(result) else result
                results.append({'event': index, 'mode': mode, 'result': result})
                record('jev_return', event=index, mode=mode)
            except Exception as exc:
                results.append({'event': index, 'mode': mode, 'error': str(exc)})
        async def judge(index, event, mode):
            # Independent replay deliberately serializes each gate. Host collision
            # must be tested through its scheduler, not unsafe concurrent gate calls.
            async with locks[mode]:
                await judge_once(index, event, mode)
        for at, index, kind, payload in sorted(schedule, key=lambda x: (x[0], x[1])):
            await wait(at)
            if kind == 'audio':
                receipt, pcm, basis = payload
                pumps.feed(pcm)
                record('audio_received', event=index, planned_ms=at, receipt=receipt, timing_basis=basis,
                       lateness_ms=(monotonic()-origin)*1000-at if realtime else None)
            else:
                for mode in modes:
                    pending.append(asyncio.create_task(judge(index, payload, mode)))
        if realtime:
            await asyncio.sleep(observation_ms/1000)
        if pumps:
            record('eof_flush', natural_endpoint=False)
            await pumps.finish()
        if pending:
            await asyncio.gather(*pending)
    except BaseException as exc:
        status, error = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed', str(exc)
        raise
    finally:
        for task in pending:
            if not task.done(): task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if pumps and not pumps.closing:
            await pumps.finish()
        executor.shutdown(wait=True, cancel_futures=True)
        if decoder and hasattr(decoder, 'close'): decoder.close()
        for gate in gates.values():
            if hasattr(gate, 'replay_close'):
                await gate.replay_close()
        write_json(run/'events.json', events)
        write_json(run/'requests.json', requests)
        write_json(run/'results.json', {'status': status, 'error': error, 'results': results,
            'assessment': 'unscored', 'coverage': 'audio pipeline and/or independent frozen JEV; host collision not covered',
            'realtime': realtime})
    return run
