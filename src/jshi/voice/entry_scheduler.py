"""One active entry, one collision request, coalesced new speech; no lost inputs."""
import asyncio
from dataclasses import dataclass, field

from jshi.decision.material import speech_rows
from jshi.decision.policies import collision_action


@dataclass
class EntryJob:
    rows: tuple
    valid: bool = True
    revision: int = 0
    approved: asyncio.Event = field(default_factory=asyncio.Event)
    superseded: asyncio.Event = field(default_factory=asyncio.Event)

    async def permit(self):
        await self.approved.wait()
        return self.valid


class EntryScheduler:
    def __init__(self, service, report=None, on_stop=None):
        self.service, self.report, self.on_stop = service, report, on_stop
        self.current = None
        self.pending = []
        self.task = None
        self.closed = False

    def enabled(self):
        return not self.closed and self.service.config['mode'] != 'off' and self.service.config['collision']

    def notify(self, rows):
        job = self.current
        if job is None or not job.valid or not self.enabled():
            return
        self.pending.extend(rows)
        job.revision += 1
        if self.service.config['mode'] == 'active':
            job.approved.clear()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._decide(job))

    async def _decide(self, job):
        try:
            while job.valid and self.current is job and self.pending:
                version = job.revision
                config = dict(self.service.config)
                incoming = tuple(self.pending)
                bindings = {}
                old, new = speech_rows((job.rows, incoming), bindings=bindings)
                result = await self.service.decide('collision', {'old': old, 'new': new}, version)
                if self.report:
                    self.report('decision_collision', {'purpose': 'collision', 'status': result.status,
                        'provider': result.provider, 'model': result.model, 'elapsed_ms': result.elapsed_ms,
                        'server_ms': result.server_ms, 'reason': result.reason, 'answers': dict(result.answers),
                        'revision': version, 'scope': bindings, 'shadow': config['mode'] != 'active'})
                if version != job.revision:
                    continue  # Newer evidence arrived; never release based on A/B while C is pending.
                self.pending.clear()
                if config['mode'] != 'active' or self.service.config['mode'] != 'active':
                    break
                action = collision_action(result, config['threshold'])
                if action == 'continue':
                    job.approved.set()
                else:
                    job.valid = False
                    job.superseded.set()
                    job.approved.set()  # Wake a waiting early entry callback before cancellation.
                    if action == 'stop' and self.on_stop:
                        await self.on_stop('decision_explicit_stop')
        except asyncio.CancelledError:
            raise
        except Exception:
            # Preserve and merge rather than allowing stale response delivery.
            if self.service.config['mode'] == 'active':
                job.valid = False
                job.superseded.set()
        finally:
            job.approved.set()

    async def run(self, rows, operation):
        job = EntryJob(tuple(rows))
        job.approved.set()
        self.current = job
        self.pending.clear()
        running = asyncio.create_task(operation())
        invalidated = asyncio.create_task(job.superseded.wait())
        try:
            await asyncio.wait((running, invalidated), return_when=asyncio.FIRST_COMPLETED)
            if not await job.permit():
                return None
            return await running
        finally:
            job.valid = False
            job.approved.set()
            self.current = None
            for task in (running, invalidated, self.task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(running, invalidated, *([self.task] if self.task else []), return_exceptions=True)
            self.task = None

    async def close(self):
        self.closed = True
        if self.current:
            self.current.valid = False
            self.current.superseded.set()
            self.current.approved.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
