import asyncio

from jshi.app.voice import VoiceConversation


class Host:
    def __init__(self, records):
        self.records, self.calls, self.logged = list(records), 0, []
        self.epoch, self.closed = 3, False

    async def _judge_once(self, *args):
        self.calls += 1
        return 'decision', dict(self.records.pop(0)), ()

    def record(self, kind, payload):
        self.logged.append(kind)


def judge(host):
    return asyncio.run(VoiceConversation._judge(host, (), None, False))


def test_scene_bookkeeping_and_own_stop_are_judged_again():
    host = Host([{'stale_causes': ['epoch', 'generation'], 'control_applied': True, 'applied_epoch': 3},
                 {'route': 'main', 'judged': True}])
    _, record, _ = judge(host)
    assert host.calls == 2 and host.logged == ['voice_jev_stale_retry']
    assert record['route'] == 'main' and record['stale_retry_of'] == ['epoch', 'generation']
    assert record['control_applied'] and record['applied_epoch'] == 3


def test_revised_source_or_foreign_stop_is_not_retried():
    for causes in (['source', 'generation'], ['epoch'], ['entry_job']):
        host = Host([{'stale_causes': causes}, {}])
        judge(host)
        assert host.calls == 1


def test_retry_happens_at_most_once():
    host = Host([{'stale_causes': ['generation']}, {'stale_causes': ['generation']}, {}])
    _, record, _ = judge(host)
    assert host.calls == 2 and record['stale_causes'] == ['generation']


def test_foreign_stop_during_retry_does_not_inherit_new_epoch():
    class Stopped(Host):
        async def _judge_once(self, *args):
            result = await super()._judge_once(*args)
            if self.calls == 2:
                self.epoch = 4
            return result
    host = Stopped([{'stale_causes': ['generation'], 'control_applied': True, 'applied_epoch': 3},
                    {'route': 'background'}])
    _, record, _ = judge(host)
    assert record['applied_epoch'] == 3 and record['applied_epoch'] != host.epoch
