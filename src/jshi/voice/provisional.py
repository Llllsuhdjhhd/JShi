"""Stable ASR fragments may be prepared without declaring the expression complete."""
import asyncio
from threading import Event
from time import monotonic

from jshi.decision.policies import continuation_action
from jshi.subject.preparation import prepare


class PreparationManager:
    def __init__(self, conversation):
        self.voice = conversation
        self.fragments = {}
        self.task = None
        self.cancelled = Event()
        self.analysis = None
        self.last_attempt = 0
        self.closed = False

    def cancel(self):
        self.cancelled.set()
        if self.task and not self.task.done():
            self.task.cancel()
        self.analysis = None

    def preview(self, rows):
        voice = self.voice
        config = voice.decisions.config
        if not config['preparation'] or config['mode'] != 'active' or self.closed:
            return
        if not rows:
            self.fragments.clear()
            return
        for row in rows:
            if not row.final or row.overlap or not row.text.strip():
                continue
            for span in getattr(row, 'replaces', ()):
                self.fragments.pop(tuple(span), None)
                self.cancel()
            key = (row.start_ms, row.end_ms)
            previous = self.fragments.get(key)
            if previous and previous.text != row.text:
                self.cancel()
            self.fragments[key] = row
        # Keep a single reliable sound's contiguous pending expression.
        recent = sorted(self.fragments.values(), key=lambda r:r.start_ms)
        if not recent:
            return
        last = recent[-1]
        recent = [r for r in recent if r.track_id == last.track_id and last.end_ms-r.start_ms <= 15000]
        for a, b in zip(recent, recent[1:]):
            if b.start_ms-a.end_ms > 2500:
                recent = [last]
                break
        self.fragments = {(r.start_ms,r.end_ms):r for r in recent}
        if (sum(len(r.text) for r in recent) < config['prepare_min_chars'] or voice.turn_busy
                or voice.active_turn is not None or not voice.turn_queue.empty()
                or self.task and not self.task.done() or monotonic()-self.last_attempt < 3):
            return
        # No content-based identification: only reuse an already recognized recent sound.
        known = next((u.speaker for u in reversed(voice.scene) if u.speaker.track_id == last.track_id
                      and u.speaker.status == 'recognized' and last.voiceprint_id
                      and u.speaker.voiceprint_id == last.voiceprint_id
                      and not last.identity_uncertain and not last.identity_tentative
                      and 0 <= last.start_ms-u.end_ms <= 10000), None)
        self.last_attempt = monotonic()
        self.cancelled = Event()
        self.task = asyncio.create_task(self._start(tuple(recent), known, self.cancelled))

    async def _start(self, rows, known, token):
        voice = self.voice
        try:
            result = await voice.decisions.decide('continuation', {'input':[r.text for r in rows],
                'preparation': None, 'stage': '判断是否可提前准备，原话尚未说完'})
            if (token.is_set() or result.choice('address', voice.decisions.config['threshold']) != 'direct'
                    or voice.turn_busy or voice.active_turn is not None):
                return
            version = voice.jev_scene_store.state['generation']
            loop = asyncio.get_running_loop()
            self.analysis = await loop.run_in_executor(voice.turn_executor, prepare,
                voice.process, voice.subject_id, known.object_id if known else '', rows, version,
                lambda: not token.is_set() and not voice.closed)
            if self.analysis:
                voice.record('preparation_ready', {'trace_id':self.analysis.trace_id,
                    'object_id':self.analysis.object_id,
                    'ranges':[[r.start_ms,r.end_ms] for r in self.analysis.rows],
                    'memory_refs':{f'R{i}':r.event_id for i,r in enumerate(self.analysis.memory_rows,1)}})
        except asyncio.CancelledError:
            token.set()
            raise
        except Exception as exc:
            voice.record('preparation_failed', {'error':type(exc).__name__})

    async def finish(self, envelope):
        voice = self.voice
        self.fragments.clear()
        if self.closed or voice.decisions.config['mode'] != 'active' or not voice.decisions.config['preparation']:
            self.cancel()
            return None, 'normal'
        if self.task and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), .8)
            except asyncio.TimeoutError:
                self.cancel()
                return None, 'normal'
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                self.cancel()
                return None, 'normal'  # Cancelling the child must not kill the formal turn worker.
        analysis, self.analysis = self.analysis, None
        if analysis is None:
            return None, 'normal'
        rows = envelope.current_utterances
        if not analysis.compatible(voice.subject_id, envelope.speaker.object_id if analysis.object_id else '',
                                   rows, voice.jev_scene_store.state['generation']):
            return None, 'normal'
        # Anonymous preparation has no private memories and must remain single-track.
        if len({r.speaker.object_id for r in rows}) != 1:
            return None, 'normal'
        validator = getattr(voice.process.memory, 'validate_prepared', None)
        try:
            if analysis.memory_versions and (not validator or not await asyncio.to_thread(validator, analysis.memory_versions)):
                return None, 'normal'
            result = await voice.decisions.decide('continuation', {**analysis.model_material(),
                'input':[r.text for r in rows], 'stage':'完整输入到齐，判断前段成果能否复用'})
        except Exception:
            return None, 'normal'
        action = continuation_action(result, voice.decisions.config['threshold'])
        voice.record('decision_continuation', {'purpose': 'continuation', 'status': result.status,
            'provider': result.provider, 'model': result.model, 'elapsed_ms': result.elapsed_ms,
            'server_ms': result.server_ms, 'reason': result.reason, 'answers': dict(result.answers),
            'action': action})
        # Formal JEV1 already authorized this range. Only an explicit restart is spoken.
        if action in {'reuse', 'revise'}:
            return analysis, action
        if action == 'restart':
            return None, 'restart'
        return None, 'normal'

    async def close(self):
        self.closed = True
        self.cancel()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)
