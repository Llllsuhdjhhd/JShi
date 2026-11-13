"""Deliver tool estimates without polling or blocking the voice event loop."""
import asyncio
import concurrent.futures
import logging


class EstimateBridge:
    def __init__(self, conversation, service):
        self.conversation, self.service = conversation, service
        self.loop = asyncio.get_running_loop()
        self.proposals = set()
        self.closed = False
        self.pending = set()
        service.subscribe_estimates(self.announce)

    def announce(self, subject, owner, task_id, preview):
        if self.closed:
            return
        try:
            same_loop = asyncio.get_running_loop() is self.loop
        except RuntimeError:
            same_loop = False
        if same_loop:
            task = self.loop.create_task(self.show(subject, owner, task_id, preview))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)
        else:
            future = asyncio.run_coroutine_threadsafe(self.show(subject, owner, task_id, preview), self.loop)
            try:
                future.result(timeout=2)
            except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
                future.cancel()

    async def show(self, subject, owner, task_id, preview):
        task = asyncio.current_task()
        self.pending.add(task)
        try:
            await self._show(subject, owner, task_id, preview)
        except Exception:
            logging.getLogger(__name__).exception('Tool estimate display failed')
        finally:
            self.pending.discard(task)

    async def _show(self, subject, owner, task_id, preview):
        c = self.conversation
        if self.closed or c.closed or c.output_disconnected or subject != c.subject_id:
            return
        if not any(u.speaker.object_id == owner and u.speaker.status in {'recognized', 'introduced'}
                   for u in tuple(c.scene)):
            return
        if preview['need_confirm']:
            self.proposals.add(task_id)
        await c.emit('tool_estimate', task_id=task_id, text=preview['text'], need_confirm=preview['need_confirm'])

    async def approve(self, task_id):
        if self.closed or task_id not in self.proposals:
            return
        self.proposals.remove(task_id)
        accepted = await asyncio.to_thread(self.service.approve, task_id)
        await self.conversation.emit('notice', text='已确认，按原有预算限制继续执行。' if accepted else '该任务已失效或达到停止上限，未启动执行。')

    async def close(self):
        self.closed = True
        self.service.unsubscribe_estimates(self.announce)
        self.proposals.clear()
        for task in tuple(self.pending):
            task.cancel()
        await asyncio.gather(*tuple(self.pending), return_exceptions=True)
