import threading

import pytest

from jshi.tool import HangStore, StubEngine, ToolModule, ToolRunner, ToolService


class Links:
    def __init__(self):
        self.owner = 'a'
        self.single_reads = self.snapshot_reads = 0

    def project(self, subject, person, refs):
        self.single_reads += 1
        return self.owner if person == 'old' else person

    def projector(self, subject):
        self.snapshot_reads += 1
        owner = self.owner
        return lambda person, refs: owner if person == 'old' else person


def test_brief_reuses_identity_view_and_refreshes_next_read(tmp_path):
    hang = HangStore(tmp_path / 'hang.jsonl')
    service = ToolService(hang, ToolRunner(ToolModule(StubEngine()), hang))
    links = Links()
    hang.person_links = service.intake_store.person_links = links
    records = [hang.create(subject_id='s', object_id='old', need='report', command='report',
                          field_ref={'activity_id': f'a{i}'}) for i in range(40)]
    for record in records:
        hang.set_wrap(record.task_id, visible=True, summary='first result', terminal=True)
        service.work_index.create(record.task_id, 's', 'old', 'report')
        service.work_index.bind(record.task_id, record.task_id)
    service.work_index.person_owner = lambda row: hang.get(next(iter(row['tasks'].values()))).object_id
    first = service.list_tool_related_entries('s', 'a')
    assert first and all(e['final_result'] == 'first result' for e in first)
    assert links.single_reads == 0
    assert links.snapshot_reads == 2  # tasks and intake each read links once, not per item

    links.owner = 'b'
    hang.set_wrap(records[0].task_id, visible=True, summary='updated result', terminal=True)
    assert service.list_tool_related_entries('s', 'a') == ()
    updated = service.list_tool_related_entries('s', 'b', required_ids=(records[0].task_id,))
    assert updated[0]['final_result'] == 'updated result'
    assert links.single_reads == 0


def test_snapshot_is_scoped_and_does_not_block_feedback_writer(tmp_path):
    hang = HangStore(tmp_path / 'hang.jsonl')
    record = hang.create(subject_id='s', object_id='a', need='report')
    with pytest.raises(RuntimeError):
        with hang.read_snapshot('s'):
            writer = threading.Thread(target=hang.set_wrap, args=(record.task_id,),
                                      kwargs={'visible': True, 'summary': 'ready', 'terminal': True})
            writer.start()
            writer.join(2)
            assert not writer.is_alive()
            assert hang.get(record.task_id).summary == ''
            raise RuntimeError('leave read')
    assert hang.get(record.task_id).summary == 'ready'


def test_owner_projection_keeps_mutations_live_and_refreshes(tmp_path):
    hang = HangStore(tmp_path / 'hang.jsonl')
    links = Links()
    hang.person_links = links
    record = hang.create(subject_id='s', object_id='old', need='report')
    with hang.owner_projection('s'):
        for _ in range(20):
            assert hang.get(record.task_id).object_id == 'a'
        hang.set_wrap(record.task_id, visible=True, summary='new', terminal=True)
        assert hang.get(record.task_id).summary == 'new'
    assert links.snapshot_reads == 1 and links.single_reads == 0
    links.owner = 'b'
    with hang.owner_projection('s'):
        assert hang.get(record.task_id).object_id == 'b'
    assert links.snapshot_reads == 2


def test_background_claim_does_not_block_and_cancel_releases():
    import asyncio
    from types import SimpleNamespace
    from jshi.app.voice import VoiceConversation
    started, finish = threading.Event(), threading.Event()
    released = []
    def claim(subject, owner):
        started.set()
        assert finish.wait(3)
        return ('notice',)
    async def run():
        service = SimpleNamespace(claim_notices=claim, release_notices=released.extend)
        task = asyncio.create_task(VoiceConversation._claim_tool_notices(
            SimpleNamespace(subject_id='s'), service, 'a'))
        try:
            for _ in range(100):
                if started.is_set(): break
                await asyncio.sleep(.01)
            assert started.is_set() and not task.done()
            task.cancel()
            await asyncio.sleep(.01)
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert released == ['notice']
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_claim_notices_reuses_links_and_preserves_claim_release(tmp_path):
    hang = HangStore(tmp_path / 'hang.jsonl')
    service = ToolService(hang, ToolRunner(ToolModule(StubEngine()), hang))
    links = Links()
    hang.person_links = links
    records = [hang.create(subject_id='s', object_id='old', need='report') for _ in range(20)]
    for r in records:
        service.work_index.create(r.task_id, 's', 'old', 'report')
        service.work_index.bind(r.task_id, r.task_id)
        service.work_index.notice(r.task_id, r.task_id, 'result_ready')
    service.work_index.person_owner = lambda row: hang.get(next(iter(row['tasks'].values()))).object_id
    ids = service.claim_notices('s', 'a')
    assert ids and links.single_reads == 0 and links.snapshot_reads == 1
    assert not set(ids) & set(service.claim_notices('s', 'a'))
    service.release_notices(ids)
    assert set(ids) <= set(service.claim_notices('s', 'a'))
    links.owner = 'b'
    service.release_notices(ids)
    assert service.claim_notices('s', 'a') == ()
    assert service.claim_notices('s', 'b')
