"""Acoustic completion of cloud transcripts; no second ASR or six-second wait."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from .contracts import SpeechSegment
from .turns import TurnCoordinator


class OnlineTurnGate:
    def __init__(self, audio, policy, detector, accept, *, report=None,
                 urgent=lambda: False, timeout_s=.6, max_hold_s=2):
        self.audio, self.detector, self.accept = audio, detector, accept
        self.report, self.urgent = report, urgent
        self.coordinator = TurnCoordinator(policy, timeout_s=timeout_s, max_hold_s=max_hold_s)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jshi-online-turn')
        self.lock = asyncio.Lock()
        self.closed = False
        self.task = asyncio.create_task(self._poll())

    def feed(self, pcm):
        if not self.closed:
            windows = self.detector.feed(pcm)
            # Record earlier local boundaries separately from cloud finalization.
            # A VAD window is evidence, not permission to commit provisional text.
            if self.report:
                for samples, start_ms in windows or ():
                    self.report('online_acoustic_window', {
                        'start_ms': start_ms, 'end_ms': start_ms + len(samples)/16,
                        'input_audio_ms': getattr(self.detector, 'total', 0)/16,
                        'semantic_complete': None,
                        'basis': 'local_vad_not_cloud_final'})

    def speech_active(self):
        return self.detector.speech_active()

    async def _deliver(self, rows, timings):
        for row, timing in zip(rows, timings):
            if self.report:
                self.report('online_acoustic_turn', {k:v for k,v in timing.items() if not k.startswith('_')})
            if not self.closed:
                await self.accept(row, **timing.get('_arrival', {}))

    async def submit(self, row, **arrival):
        if self.closed:
            return
        if not row.final:
            await self.accept(row, **arrival)
            return
        async with self.lock:
            # Intervening speech closes the previous acoustic turn before a later
            # utterance from that speaker can reuse it. Unknown short replies are
            # evidence of a boundary, not permission to bind a new named person.
            incoming = self.coordinator.key(row)
            ready, timings = [], []
            for key, pending in list(self.coordinator.pending.items()):
                previous = pending.rows[-1]
                chronological = row.start_ms >= previous.end_ms
                changed = incoming is not None and incoming != key
                # Unknown identity does not prove a new person, even on the
                # same provider track. Release the earlier fragment first;
                # never let the later unresolved fragment overtake it or borrow
                # a named identity solely from its neighbouring utterance.
                unknown_other = incoming is None
                if chronological and not row.replaces and (changed or unknown_other):
                    reason = 'unresolved_continuation_boundary' if unknown_other else 'intervening_speech'
                    self.coordinator._release(key, ready, timings, reason)
            await self._deliver(ready, timings)
            if self.urgent():
                ready, timings = [], []
                self.coordinator.poll(ready, timings, final=True)
                await self._deliver(ready, timings)
                await self.accept(row, **arrival)
                return
            pcm = self.audio.segment(row.start_ms, row.end_ms)
            if row.replaces:
                ready, timings = [], []
                self.coordinator.poll(ready, timings, final=True)
                await self._deliver(ready, timings)
            if pcm is None or row.overlap or row.replaces or self.coordinator.key(row) is None:
                await self.accept(row, **arrival)
                return
            import numpy as np
            samples = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
            segment = SpeechSegment(samples, row.track_id, row.start_ms, row.end_ms,
                row.overlap, row.voiceprint_id, row.confidence,
                speaker_cluster_id=row.speaker_cluster_id)
            timing = {'start_ms': row.start_ms, 'end_ms': row.end_ms,
                      '_asr_return_mono': arrival.get('asr_received_mono', monotonic()),
                      '_arrival': arrival}
            allow_expiry = not self.speech_active()
            def process():
                ticket = self.coordinator.begin([segment])
                return self.coordinator.finish(ticket, [segment], [row], [timing], allow_expiry=allow_expiry)
            ready, timings = await asyncio.get_running_loop().run_in_executor(self.executor, process)
            if not self.closed:
                if self.report:
                    for event in self.coordinator.last_events:
                        self.report('online_acoustic_decision', dict(event))
                await self._deliver(ready, timings)

    async def flush(self):
        async with self.lock:
            ready, timings = [], []
            self.coordinator.poll(ready, timings, final=True)
            await self._deliver(ready, timings)

    async def _poll(self):
        while not self.closed:
            await asyncio.sleep(.05)
            async with self.lock:
                ready, timings = [], []
                self.coordinator.poll(ready, timings, final=self.urgent(),
                                      allow_expiry=not self.speech_active(), hard_max_hold=True)
                await self._deliver(ready, timings)

    async def close(self):
        self.closed = True
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
        self.coordinator.reset()
        await asyncio.to_thread(self.coordinator.close)
        close_policy = getattr(self.coordinator.policy, 'close', None)
        if close_policy is not None:
            await asyncio.to_thread(close_policy)


def build_online_turn_gate(config, audio, accept, root, *, policy=None, **kwargs):
    from .segmentation import SileroSegmenter
    from .turns import build_turn_policy
    from .semantic_turn import build_semantic_policy
    from .online_completion import OnlineCompletionPolicy
    import logging
    import sherpa_onnx
    vad = sherpa_onnx.VadModelConfig()
    vad.silero_vad.model = str(root/'silero_vad.onnx')
    vad.silero_vad.min_silence_duration = config.end_window_ms/1000
    vad.sample_rate = 16000
    detector = SileroSegmenter(sherpa_onnx.VoiceActivityDetector(vad, buffer_size_in_seconds=30))
    semantic = None
    try:
        semantic = build_semantic_policy(config, root)
    except Exception:
        logging.getLogger(__name__).exception('Online semantic correction unavailable; conflicts release as unknown')
    combined = OnlineCompletionPolicy(policy if policy is not None else build_turn_policy(config, root), semantic,
                                     timeout_s=config.semantic_turn_timeout_s)
    return OnlineTurnGate(audio, combined, detector, accept,
        timeout_s=config.turn_timeout_s, max_hold_s=config.turn_max_hold_s, **kwargs)
