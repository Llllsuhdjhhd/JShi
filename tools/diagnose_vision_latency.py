"""Read-only replay of saved memory candidates, with/without visual associations.

No camera, cloud calls, memory reinforcement or writes to the user's data.
This isolates the local visual overhead; it does not benchmark the real search.
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from statistics import median
from time import perf_counter
from types import SimpleNamespace

from jshi.memory.rems3 import from_recalled_fragments
from jshi.vision.store import VisionStore
from jshi.vision.memory import VisualMemory


class ReadOnlyStore(VisionStore):
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'vision.sqlite3'

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3) as c:
            c.row_factory = sqlite3.Row
            yield c


def diagnose(root, subject, repeats=3, trace_offset=0):
    root = Path(root)
    with sqlite3.connect((root/'rems/rems.db').resolve().as_uri()+'?mode=ro', uri=True, timeout=3) as c:
        row = c.execute('select items from recall_traces where subject_id=? and json_array_length(items)>0 order by created_at desc limit 1 offset ?', (subject, trace_offset)).fetchone()
    if row is None:
        raise ValueError('没有已保存的召回候选')
    raw = [SimpleNamespace(**item) for item in json.loads(row[0])]
    for item in raw:
        item.event_type = 'memory'
    fragments = from_recalled_fragments(raw)
    if not fragments:
        raise ValueError('最近的召回候选为空')
    class Backend:
        def recall(self, *args, **kwargs): return fragments
    adapter = VisualMemory(Backend(), ReadOnlyStore(root/'vision'))
    off, on, associations = [], [], 0
    for _ in range(repeats):
        start = perf_counter()
        baseline = Backend().recall(subject, '')
        off.append((perf_counter()-start)*1000)
        phase = {}
        start = perf_counter()
        augmented = adapter._recall(subject, '', phase)
        on.append((perf_counter()-start)*1000)
        assert len(augmented) == len(baseline)
        associations = sum(len(item.visual_observations) for item in augmented)
    timings = []
    for line in (root/'activity_timings.jsonl').read_text(encoding='utf-8').splitlines()[-100:]:
        try:
            item = json.loads(line)
            if item.get('subject_id') == subject and item.get('total_ms',0)>12000:
                timings.append({'started_at':item.get('started_at'), 'total_ms':item['total_ms'], 'steps':item.get('steps')})
        except ValueError:
            pass
    return {'read_only':True, 'candidate_count':len(fragments), 'association_count':associations,
            'repeats':repeats, 'vision_off_ms':round(median(off),3), 'vision_on_ms':round(median(on),3),
            'vision_on_runs_ms':[round(x,3) for x in on], 'recent_slow_turns':timings,
            'scope':'回放最近保存的候选（日志可能只保留60条）；不测真实检索、模型、并发写入或声音队列。不能仅凭此排除视觉。'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default='.jshi')
    parser.add_argument('--subject', default='stone')
    parser.add_argument('--output', required=True)
    parser.add_argument('--trace-offset', type=int, default=0)
    args = parser.parse_args()
    report = diagnose(args.data_dir, args.subject, trace_offset=args.trace_offset)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'recent_slow_turns','scope'}}, ensure_ascii=False))
