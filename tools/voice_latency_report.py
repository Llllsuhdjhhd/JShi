"""Read-only summary of voice stage latency, grouped by day or session.

Stages per input, all on the server monotonic clock:
  audio_end_to_text    browser audio end received -> cloud final returned
  text_to_jev_start    cloud final returned -> first entry JEV call started
  jev_span             first JEV start -> last JEV finish (re-judgments included)
  jev_to_main          last JEV finish -> main cognition started
Event-loop stalls come from voice_diagnostics.jsonl.
"""
import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path

STAGES = ('audio_end_to_text_ms', 'text_to_jev_start_ms', 'jev_span_ms', 'jev_to_main_ms', 'text_to_main_ms')


def rows(path):
    if not path.exists():
        return
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def quantile(values, q):
    values = sorted(values)
    return round(values[min(len(values)-1, int(len(values)*q))]) if values else None


def report(root, by, since):
    groups = defaultdict(lambda: defaultdict(list))
    def key(row):
        return row.get('session_id', '')[:8] if by == 'session' else datetime.fromtimestamp(row['at_ms']/1000).strftime('%m-%d')
    for row in rows(root/'voice_latency.jsonl'):
        if row.get('stage') != 'main_started' or row.get('at_ms', 0) < since:
            continue
        group = groups[key(row)]
        for stage in STAGES:
            if row.get(stage) is not None:
                group[stage].append(row[stage])
        group['jev_rounds'].append(row.get('jev_rounds', 0))
    for row in rows(root/'voice_diagnostics.jsonl'):
        if row.get('event_type') == 'event_loop_lag' and row.get('at_ms', 0) >= since:
            groups[key(row)]['loop_stall_max_ms'].append(row['max_ms'])
    for name in sorted(groups):
        group = groups[name]
        print(f'{name}  inputs={len(group["jev_rounds"])}')
        for stage in (*STAGES, 'jev_rounds', 'loop_stall_max_ms'):
            values = group.get(stage)
            if values:
                print(f'  {stage:22} n={len(values):4} p50={quantile(values, .5)} p90={quantile(values, .9)} max={round(max(values))}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.jshi')
    parser.add_argument('--by', choices=('day', 'session'), default='day')
    parser.add_argument('--since', default='', help='YYYY-MM-DD HH:MM, local time')
    args = parser.parse_args()
    since = datetime.strptime(args.since, '%Y-%m-%d %H:%M').timestamp()*1000 if args.since else 0
    report(Path(args.root), args.by, since)
