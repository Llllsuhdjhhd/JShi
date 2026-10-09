"""Read-only aggregate review; never prints saved pictures or conversation text."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from statistics import median


def review(root):
    root = Path(root)
    with sqlite3.connect((root/'vision/vision.sqlite3').resolve().as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        snapshots = list(db.execute('select captured,completed,reason,model,described_at from snapshots order by completed desc limit 500'))
        errors = list(db.execute('select at,message from errors order by at desc limit 100'))
        calls = [r[0] for r in db.execute('select at from calls order by at desc limit 500')]
    with sqlite3.connect((root/'subject.sqlite3').resolve().as_uri()+'?mode=ro', uri=True) as db:
        turns = [json.loads(r[0]) for r in db.execute(
            "select content from histories where event_type='voice_turn_diagnostics' order by sequence desc limit 40")]
    voice_timings = []
    for turn in turns:
        timing = turn.get('timing',{}).get('voice',{})
        voice_timings.append({'activity_id':turn.get('activity_id'),
            **{k:timing.get(k) for k in ('asr_lag_ms','queue_wait_ms','speech_collection_ms','input_worker_wait_ms',
                'ready_queue_wait_ms','current_jev_ms','model_to_reply_ms','speech_to_reaction_ms','received_to_reaction_ms')},
            'input_count':len(turn.get('input',{}).get('current_utterances',[]))})
    cloud = [r for r in snapshots if r['model'] and r['model'] != 'local']
    lag = [(r['completed']-r['described_at'])*1000 for r in cloud if r['described_at']]
    def error_category(text):
        for token in ['402','401','429','timeout','timed out','超时','调用上限','截断','未返回有效描述','配置']:
            if token in text: return token
        return 'other'
    activities = [json.loads(l) for l in (root/'activity_timings.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()][-100:]
    phases = [json.loads(l) for l in (root/'memory_phase_timings.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()][-100:]
    recent = activities[-20:]
    return {'read_only':True, 'snapshot_sample_count':len(snapshots),
        'snapshot_reasons':dict(Counter(r['reason'] for r in snapshots)),
        'cloud_observation_age_at_completion_ms':{'count':len(lag),'median':round(median(lag),1) if lag else None,'max':round(max(lag),1) if lag else None},
        'recent_error_categories':dict(Counter(error_category(r['message']) for r in errors)),
        'last_error_at':datetime.fromtimestamp(errors[0]['at'],timezone.utc).isoformat() if errors else None,
        'recent_calls_count':len(calls),
        'last_call_at':datetime.fromtimestamp(calls[0],timezone.utc).isoformat() if calls else None,
        'latest_20_turns':{'count':len(recent),'median_ms':median(r['total_ms'] for r in recent) if recent else None,
            'slow_over_12s':[{k:r.get(k) for k in ('started_at','total_ms','steps')} for r in recent if r['total_ms']>12000]},
        'memory_phases':[{k:r.get(k) for k in ('started_at','operation','status','total_ms','backend_ms','visual_associations_ms','visual_prepare_ms')} for r in phases],
        'voice_timings':voice_timings,
        'scope':'最近500个视觉快照、100个错误、100个阶段记录；快照完成滞后包含排队，不等于API耗时。'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',default='.jshi')
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    report = review(args.data_dir)
    output = Path(args.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'memory_phases','voice_timings','scope'}},ensure_ascii=False))
