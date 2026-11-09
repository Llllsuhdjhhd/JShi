from collections import deque
from types import SimpleNamespace

import pytest

from jshi.app.voice import VoiceConversation
from jshi.voice.entry_queue import take_group


def group(iid, stamp):
    utterance = SimpleNamespace(input_id=iid, text='一句话', start_ms=0, end_ms=100,
                                speaker=SimpleNamespace(track_id='speaker'))
    return {'current': [utterance], 'track_id': 'speaker', 'queued': 0,
            'queued_at': stamp, 'collection_trigger': 'revision', 'expression_complete': None}


def test_revision_uses_its_own_enqueue_clock(monkeypatch):
    conversation = VoiceConversation.__new__(VoiceConversation)
    conversation.process = SimpleNamespace(profiles=SimpleNamespace())
    conversation.input_records = {'same': {'judge_queued_at': 100.0}}
    conversation.judge_pending = deque([group('same', 107.54)])
    events = []
    conversation.record = lambda kind, data: events.append((kind, data))
    monkeypatch.setattr('jshi.app.voice.monotonic', lambda: 107.55)
    conversation._take_judge_group()
    event = next(data for kind, data in events if kind == 'voice_input_stage')
    assert event['queue_wait_ms'] == pytest.approx(10.0)


def test_merged_groups_keep_individual_clocks_when_requeued():
    pending = deque([group('a', 100.0), group('b', 102.0)])
    batch = take_group(pending, now=103.0)
    assert batch['queued_at_by_input'] == {'a': 100.0, 'b': 102.0}
    # Deferred groups preserve their real wait, while new revisions get new stamps.
    pending.extend([batch, group('c', 105.0)])
    assert take_group(pending, now=106.0)['queued_at_by_input'] == {
        'a': 100.0, 'b': 102.0, 'c': 105.0}
    pending.append(group('a', 108.0))
    assert take_group(pending, now=108.1)['queued_at_by_input'] == {'a': 108.0}
