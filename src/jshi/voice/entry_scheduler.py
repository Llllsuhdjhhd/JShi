"""One active entry, one collision request, coalesced new speech; no lost inputs."""
import asyncio
from dataclasses import dataclass, field, replace

from jshi.decision.material import speech_rows
from jshi.voice.turn_schedule import Effect, TurnSchedule


@dataclass
class EntryJob:
    rows: tuple
    valid: bool = True
    revision: int = 0
    execution: str = 'open'
    approved: asyncio.Event = field(default_factory=asyncio.Event)
    superseded: asyncio.Event = field(default_factory=asyncio.Event)

    async def permit(self):
        await self.approved.wait()
        return self.valid and self.execution != 'revoked'


class EntryScheduler:
    def __init__(self, service, report=None, on_stop=None, schedule=None):
        self.service, self.report, self.on_stop = service, report, on_stop
        self.schedule = schedule or TurnSchedule(speech_hold=bool(service.config.get('speech_hold', False)))
        self.current = None
        self.pending = []
        self.task = None
        self.closed = False
        self.effects = []
        self.attempt_id = ''
        self.on_ready = None
        self.deferred_group = None
        self.releases = set()
        self._scope = None
        self.running_calls = lambda: ()
        self.work_busy = lambda: False
        self.work_context = lambda: ()
        self.on_effect = None
        self.work_pending = []
        self.work_task = None
        self.work_revision = 0
        self.decide_tasks = set()

    def enabled(self):
        return not self.closed and self.service.config['mode'] != 'off' and self.service.config['collision']

    def applies(self, config=None):
        """Only an applied collision may hold old work; recorded calls never delay it."""
        config = self.service.config if config is None else config
        return config['mode'] == 'active' and bool(config.get('collision_apply', False))

    def take_effect(self):
        return self.effects.pop() if self.effects else None

    def notify(self, rows):
        self.schedule.speech_hold = bool(self.service.config.get('speech_hold', False))
        job = self.current
        if not self.enabled():
            return
        if job is None or not job.valid:
            if self.applies():
                for row in rows:
                    self.schedule.note_text(getattr(row, 'input_id', id(row)), getattr(row, 'text', ''))
            if self.work_busy():
                self.work_pending.extend(rows)
                self.work_revision += 1
                if self.work_task is None or self.work_task.done():
                    self.work_task = asyncio.create_task(self._judge_work_input())
            return
        self.pending.extend(rows)
        job.revision += 1
        active = self.applies()
        if active:
            for row in rows:
                effect = self.schedule.note_text(getattr(row, 'input_id', id(row)), getattr(row, 'text', ''),
                                                 active_mode=True)
                if effect.kind == 'hold_submission':
                    job.execution = 'held'
        self._scope = self.schedule.capture()
        if self.applies():
            job.approved.clear()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._decide(job))
            self.decide_tasks.add(self.task)
            self.task.add_done_callback(self.decide_tasks.discard)

    async def _judge_work_input(self):
        """A separate collision call while cognition/preparation/player is occupied."""
        retries = 0
        while self.work_pending and not self.closed:
            incoming = tuple(self.work_pending)
            revision = self.work_revision
            scope = self.schedule.capture()
            matter = self.schedule.current()
            matter_id = matter.matter_id if matter else ''
            config = dict(self.service.config)
            ids = {r.input_id for r in incoming}
            old, new = speech_rows((tuple(r for r in self.work_context() if r.input_id not in ids), incoming))
            try:
                result = await self.service.decide('collision', {'old': old, 'new': new}, revision)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.work_pending.clear()
                await self._keep_work(matter, ids)
                return
            if self.report:
                self.report('decision_work_collision', {'revision': revision, 'status': result.status,
                    'answers': dict(result.answers), 'shadow': not self.applies(config)})
            if revision != self.work_revision:
                retries += 1
                if retries >= 3:
                    # The queued speech is still judged by its own entry; old work stays valid.
                    ids |= {r.input_id for r in self.work_pending}
                    self.work_pending.clear()
                    await self._keep_work(matter, ids)
                    return
                continue
            self.work_pending.clear()
            if (not self.applies(config) or not self.applies()
                    or self.schedule.current() is not matter or not self.work_busy()):
                continue
            effect = self.schedule.apply_work_collision(
                result.choice('relation', config['threshold']),
                result.choice('old_work', config['threshold']), scope, matter_id=matter_id)
            if self.on_effect:
                await self.on_effect(replace(effect, covered=tuple(ids)))

    async def _keep_work(self, matter, ids):
        if (not self.on_effect or not self.applies() or matter is None
                or self.schedule.current() is not matter):
            return
        effect = self.schedule.keep_after_unclear(matter_id=matter.matter_id)
        await self.on_effect(replace(effect, covered=tuple(ids)))

    async def _decide(self, job):
        retries = 0
        try:
            while job.valid and job.execution != 'revoked' and self.current is job and self.pending:
                version = job.revision
                config = dict(self.service.config)
                incoming = tuple(self.pending)
                scope = self.schedule.capture()
                bindings = {}
                old, new = speech_rows((job.rows, incoming), bindings=bindings)
                result = await self.service.decide('collision', {'old': old, 'new': new}, version)
                if self.report:
                    self.report('decision_collision', {'purpose': 'collision', 'status': result.status,
                        'provider': result.provider, 'model': result.model, 'elapsed_ms': result.elapsed_ms,
                        'server_ms': result.server_ms, 'reason': result.reason, 'answers': dict(result.answers),
                        'revision': version, 'scope': bindings, 'shadow': not self.applies(config)})
                relation = result.choice('relation', config['threshold'])
                work = result.choice('old_work', config['threshold'])
                if version != job.revision:
                    retries += 1
                    if retries < 3:
                        continue  # Newer evidence arrived; never release based on A/B while C is pending.
                    # Speech keeps changing: no relation covers all of it, so the old entry stays.
                    incoming = tuple(self.pending)
                    scope = self.schedule.capture()
                    relation = work = 'uncertain'
                if self.current is not job:
                    break  # A recorded-only call outlived its entry; pending now belongs to the next one.
                self.pending.clear()
                if not self.applies(config) or not self.applies():
                    break
                covered = tuple(getattr(row, 'input_id', id(row)) for row in (*job.rows, *incoming))
                effect = self.schedule.apply_collision(relation, work, covered, scope,
                                                       mode='active', attempt_id=self.attempt_id)
                if effect.kind == 'stale' and not self.pending:
                    # Inputs outside this decision are judged by their own entries.
                    effect = self.schedule.keep_after_unclear(attempt_id=self.attempt_id)
                self.effects.append(effect)
                if effect.kind == 'reapprove':
                    job.execution = 'open'
                    job.approved.set()
                elif effect.kind == 'capped':
                    job.execution = 'held'
                    break
                elif effect.kind in {'replace_entry', 'cancel'}:
                    job.execution = 'revoked'
                    job.valid = False
                    job.superseded.set()
                    job.approved.set()
                elif effect.kind == 'diagnostic':
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed collision call says nothing about the new speech; keep the old entry.
            # The new speech is still judged by its own entry. Do not touch playback from here.
            if self.current is job:
                self.pending.clear()
            if self.applies() and self.current is job and job.valid:
                effect = self.schedule.keep_after_unclear(attempt_id=self.attempt_id)
                self.effects.append(effect)
                if effect.kind == 'reapprove':
                    job.execution = 'open'
        finally:
            if job.execution != 'held':
                job.approved.set()

    async def run(self, rows, operation):
        covers = tuple(getattr(row, 'input_id', id(row)) for row in rows)
        attempt = self.schedule.begin_attempt(covers)
        if attempt is None:
            self.effects.append(Effect('capped'))
            return None
        self.attempt_id = attempt.attempt_id
        self._scope = self.schedule.capture()
        job = EntryJob(tuple(rows))
        job.approved.set()
        self.current = job
        self.pending.clear()
        running = asyncio.create_task(operation())
        invalidated = asyncio.create_task(job.superseded.wait())
        try:
            await asyncio.wait((running, invalidated), return_when=asyncio.FIRST_COMPLETED)
            if job.execution == 'held' and running.done() and not invalidated.done() and self.task and not self.task.done():
                # The result came first; wait for the collision relation instead of discarding it.
                await asyncio.wait((asyncio.shield(self.task), invalidated), return_when=asyncio.FIRST_COMPLETED)
            if job.execution == 'held' and running.done() and not invalidated.done():
                job.execution = 'revoked'
                job.valid = False
                job.approved.set()
                self.effects.append(Effect('replace_entry', attempt_id=attempt.attempt_id, grade='reconstruct'))
                return None
            if not await job.permit():
                return None
            return await running
        finally:
            job.approved.set()
            self.current = None
            if not attempt.orphan:
                self.schedule.attempt_finished(attempt.attempt_id)
            else:
                calls = tuple(self.running_calls())
                release = asyncio.create_task(self._release_orphan(attempt.attempt_id, calls))
                self.releases.add(release)
                release.add_done_callback(self.releases.discard)
            # A recorded-only call finishes on its own deadline so the sample keeps its answer.
            observed = self.task if self.task is not None and not self.applies() else None
            stopping = [task for task in (running, invalidated, self.task) if task is not None and task is not observed]
            for task in stopping:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*stopping, return_exceptions=True)
            self.task = None

    async def _release_orphan(self, attempt_id, calls):
        try:
            await asyncio.gather(*(asyncio.shield(call) for call in calls), return_exceptions=True)
        except asyncio.CancelledError:
            return
        self.schedule.attempt_finished(attempt_id)
        if self.on_ready:
            await self.on_ready()

    async def close(self):
        self.closed = True
        if self.current:
            self.current.valid = False
            self.current.execution = 'revoked'
            self.current.superseded.set()
            self.current.approved.set()
        tasks = [self.task, self.work_task, *self.releases, *self.decide_tasks]
        for task in tasks:
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)
