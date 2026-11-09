"""Bounded entry batches; a scheduling hint never decides whether to respond."""
import re
from time import monotonic


def addressed(group, names=('匠石',)):
    if group.get('collection_trigger') == 'active_control':
        return True
    return any(re.match(r'^\s*(?:' + '|'.join(re.escape(n) for n in names if n) + r')[，,：:\s！？!?]', u.text)
               for u in group['current'])


def take_group(pending, *, names=('匠石',), now=None, max_rows=6, max_chars=1200):
    """Prioritize a named call, retaining queued predecessors from the same voice.

    Eight-second aging prevents indefinite postponement. Inputs are never dropped;
    queued accounting moves with each whole group, including duplicate revisions.
    """
    now = monotonic() if now is None else now
    groups = list(pending)
    selected = 0
    if now - groups[0].get('queued_at', now) < 8:
        selected = next((i for i, g in enumerate(groups) if addressed(g, names)), 0)
    tracks = {u.speaker.track_id for u in groups[selected]['current'] if u.speaker.track_id}
    # Do not move a follow-up ahead of its own unjudged premise.
    selected = next((i for i, g in enumerate(groups[:selected])
                     if any(u.speaker.track_id in tracks for u in g['current'])), selected)
    first = groups.pop(selected)
    chosen = [first]
    count = len(first['current'])
    chars = sum(len(u.text) for u in first['current'])
    # An urgent batch stays small; unrelated backlog is handled by the next batch.
    urgent = addressed(first, names)
    tracks = {u.speaker.track_id for u in first['current'] if u.speaker.track_id}
    while groups and count + len(groups[0]['current']) <= max_rows:
        if urgent and not any(u.speaker.track_id in tracks for u in groups[0]['current']):
            break  # A same-voice correction must stay with its named premise.
        size = sum(len(u.text) for u in groups[0]['current'])
        if chars + size > max_chars:
            break
        more = groups.pop(0)
        chosen.append(more)
        count += len(more['current'])
        chars += size
        if addressed(more, names):
            break
    pending.clear()
    pending.extend(groups)
    result = dict(first)
    # Enqueue clocks belong to queue entries, not mutable input records: the same
    # input can return with revised text long after its first judgment finished.
    clocks = {}
    for group in chosen:
        saved = group.get('queued_at_by_input', {})
        for utterance in group['current']:
            stamp = saved.get(utterance.input_id, group.get('queued_at'))
            if stamp is not None:
                clocks[utterance.input_id] = min(clocks.get(utterance.input_id, stamp), stamp)
    result['queued_at_by_input'] = clocks
    result['queued'] = sum(g['queued'] for g in chosen)
    result['current'] = sorted((u for g in chosen for u in g['current']), key=lambda u: (u.start_ms, u.end_ms))
    last = next((g for g in reversed(chosen) if g['collection_trigger'] == 'active_control'), chosen[-1])
    result.update(track_id=last['track_id'], collection_trigger=last['collection_trigger'],
                  expression_complete=last['expression_complete'])
    return result
