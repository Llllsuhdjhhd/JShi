"""Bounded replay of saved JEV requests; never opens a live memory backend.

Run with --live for at most twelve new model calls (two repeats by default).
Original logged decisions are the baseline, not a claim of randomized A/B testing.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys
from time import time, monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from jshi.app.cli import _load_local_env
from jshi.core.skillconfig import SkillConfigStore, build_model_port
from jshi.models.prompt import build_system, build_user
from jshi.voice.jev import VoiceJEV
from jshi.voice.jev_scene import JEVSceneWriter, recent_material


def payload(call):
    text = call['user_text']
    return json.loads(text[text.index('{'):])


def restore_clock(value, anchor, key=''):
    if isinstance(value, dict):
        return {k: restore_clock(v, anchor, k) for k, v in value.items() if k != 'time_basis'}
    if isinstance(value, list):
        return [restore_clock(v, anchor, key) for v in value]
    if type(value) in (float, int):
        if key in {'at', 'at_ms', 'scene_at', 'received_ms', 'captured_start_ms', 'captured_end_ms'}:
            return anchor + value
        if key in {'captured', 'completed', 'described_at', 'observed_at', 'completed_at'}:
            return (anchor + value) / 1000
    return value


def entry_args(original):
    now = time() * 1000
    data = restore_clock(deepcopy(original), now)
    batch = []
    for line in data['input']['lines']:
        voice = line.get('continuity') if line.get('identity') == 'unresolved' else line.get('voice')
        row = {'n': line['i'], 'text': line['text'], 'who': line.get('p', 'unknown'),
               'at': line.get('at'), 'track': line.get('track'), 'source': line.get('source', 'voice'),
               'voice_evidence': voice or {}, 'joint_evidence': line.get('joint_evidence', {})}
        if line.get('name'):
            row['who'] += '（' + line['name'] + '）'
        row['candidates'] = [{**c, 'who': c['p'],
            'score': c.get('classification_score', c.get('similarity')),
            'verified': c.get('validation') != 'limited'} for c in line.get('candidates', [])]
        batch.append(row)
    delivery = {**data.get('playback', {}), 'now': now, 'jev_scene': data.get('scene', ''),
        'conversation': data.get('conversation', []), 'short_chat': {'allowed': False},
        'waiting': {'allowed': False}, 'names_for_jiangshi': data['input'].get('names', ['匠石'])}
    if data['input'].get('envelope'):
        # Text-only replay: the original request carried metadata, not audio bytes.
        delivery['envelope'] = {**data['input']['envelope'], 'parts': []}
    return batch, data['input'].get('unwritten', []), delivery


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--repeats', type=int, choices=[1, 2], default=2)
    parser.add_argument('--output', type=Path, default=ROOT / 'work/jev-context-replay-20261024.json')
    args = parser.parse_args()
    records = [json.loads(line) for line in (ROOT/'.jshi/step_inputs.jsonl').read_text(encoding='utf-8').splitlines()]
    calls = [r for r in records if r.get('kind') == 'call']
    connection = sqlite3.connect((ROOT/'.jshi/subject.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)
    diagnostics = {d.get('prompt_activity_id'): d for (text,) in connection.execute(
        "SELECT content FROM histories WHERE event_type='voice_jev_diagnostics'") for d in [json.loads(text)]}
    # Keep read-only connection until historical reconstruction is complete.
    report = {'scope': '真实历史的文字与证据回放；不重跑ASR/声纹，不产生实际播放或写入正式记忆',
              'baseline': '原始运行记录，非同步随机A/B；新版回放关闭短答与等待接话，不发送人物肖像', 'cases': [], 'scene_cases': []}
    cases = [('money_tease', '对钱很感兴趣嘛', '2026-10-24'),
             ('food_background', '我把葱吃掉', '2026-10-24'),
             ('greeting', '哈喽。', '2026-10-24'),
             ('remember_me', '你还记得我吗', '2026-10-22')]
    selected = []
    for name, text, day in cases:
        found = [r for r in calls if r.get('purpose') == 'voice_jev' and r.get('at', '').startswith(day)
                 and any(text in line['text'] for line in payload(r)['input']['lines'])]
        reconstructed = False
        if not found:
            # Older complete request logs have rolled out. Reconstruct only from
            # actual saved words, initial evidence and confirmed playback, and label it.
            matches = [(aid, d) for aid, d in diagnostics.items()
                       if any(text in part.get('text', '') for part in d.get('input_parts', []))]
            if not matches:
                raise RuntimeError(f'No saved material for {name}')
            aid, diagnostic = matches[-1]
            (at,) = connection.execute("SELECT created_at FROM histories WHERE event_type='voice_jev_diagnostics' AND content LIKE ? ORDER BY sequence DESC LIMIT 1", ('%'+aid+'%',)).fetchone()
            (raw,) = connection.execute("SELECT content FROM histories WHERE event_type='voice_scene_input' AND content LIKE ? ORDER BY sequence DESC LIMIT 1", ('%'+diagnostic['input_ids'][0]+'%',)).fetchone()
            utterance = json.loads(raw)
            previous = connection.execute("SELECT content FROM histories WHERE event_type='voice_jev_scene' AND created_at < ? ORDER BY sequence DESC LIMIT 1", (at,)).fetchone()
            scene = json.loads(previous[0]).get('scene', '') if previous else ''
            dialogue = json.loads((ROOT/'.jshi/voice_dialogue.json').read_text(encoding='utf-8'))
            cutoff = utterance['received_at_ms']
            conversation = [{'who':r['who'], 'text':r['text'], 'at':r['at']-cutoff, 'basis':'收到的原话'}
                for r in dialogue['inputs'] if cutoff-180000 < r['at'] < cutoff]
            for reply in dialogue['replies']:
                for index in reply.get('played', []):
                    played_at = reply.get('played_at', {}).get(str(index))
                    if played_at is not None and cutoff-180000 < played_at < cutoff:
                        conversation.append({'who':'匠石','text':reply['texts'][index], 'at':played_at-cutoff, 'basis':'播放器确认已播出'})
            conversation.sort(key=lambda r:r['at'])
            mark = (diagnostic.get('items') or [{}])[0]
            person = mark.get('voice_pick') or 'unknown'
            reconstructed_payload = {'now':cutoff,'scene':scene, 'input':{
                'lines':[{'i':1, 'p':person, 'text':utterance['text'], 'at':0,
                          'voice':{'pick':person,'strength':mark.get('voice_strength','unavailable'), 'basis':'历史入口记录的初始声音证据'}}]},
                'conversation':conversation[-16:], 'playback':{'state':'completed'},
                'short_chat':{'allowed':False},'waiting':{'allowed':False}}
            call = {'activity_id':aid,'at':at,'user_text':json.dumps(reconstructed_payload,ensure_ascii=False)}
            reconstructed = True
        else:
            call = next((r for r in found if r['activity_id'] == 'jev-c2d29911476e47d88836f68a235b12ec'), found[-1]) if name == 'greeting' else found[-1]
        original = diagnostics.get(call['activity_id'], {})
        case = {'name': name, 'source_activity': call['activity_id'], 'source_at': call['at'], 'reconstructed': reconstructed,
                'historical': {k: original.get(k) for k in ['action', 'control', 'front_choice', 'model_error']},
                'replays': []}
        report['cases'].append(case)
        selected.append((case, payload(call)))
    connection.close()
    # Material-level comparison is fully offline and uses complete archived records.
    window_call = next(r for r in calls if r.get('purpose') == 'voice_jev_scene'
                       and r.get('at', '').startswith('2026-10-24T05:21:35'))
    source = payload(window_call)
    archive = next((ROOT/'.jshi/jev_scenes').rglob('cf633bf9baf54c6f86ff0616668e149a.archive.jsonl'))
    omitted = next(json.loads(line)['events'] for line in archive.read_text(encoding='utf-8').splitlines()
                   if json.loads(line).get('version') == 16)
    restored = restore_clock(source, source['now'])
    events = deepcopy(omitted) + [{'id': 'included-original', 'seq': 100, 'payload': row}
                                 for row in restored['input_output']]
    snapshot = {'events': events}
    new_rows, reference = recent_material(snapshot, source['main_scene'])
    report['offline_material'] = {
        'source_activity': window_call['activity_id'], 'source_at': window_call['at'],
        'before_read': len(source['input_output']), 'before_omitted': len(omitted),
        'after_read': len(new_rows), 'after_deferred': len(snapshot['material_window']['deferred_input_ids']),
        'after_archived': len(snapshot['material_window']['archived_input_ids']),
        'after_dialogue_chars': snapshot['material_window']['chars'],
        'after_identity_chars': snapshot['material_window']['evidence_chars'],
        'after_texts': [r['text'] for r in new_rows]}
    report['prepared_requests'] = []
    class PrepareOnly:
        def generate(self, request):
            raise RuntimeError('offline request preparation; no model called')
    for case, data in selected:
        VoiceJEV(PrepareOnly(), fallback_models=[]).decide_batch('stone', *entry_args(data),
            on_request=lambda req, case=case: report['prepared_requests'].append({
                'case': case['name'], 'system': build_system(req), 'user': build_user(req)}))
    selected_scenes = []
    # Replay the exact scene input containing the money exchange, plus the
    # earlier window whose five discarded events are still in the archive.
    scene_calls = [r for r in calls if r.get('purpose') == 'voice_jev_scene']
    money = next(r for r in scene_calls if r['activity_id'] == 'jev-scene-93fbea0729404ad7aa8b2efbd38da35c')
    window = next(r for r in scene_calls if r.get('at', '').startswith('2026-10-24T05:21:35'))
    archive = next((ROOT/'.jshi/jev_scenes').rglob('cf633bf9baf54c6f86ff0616668e149a.archive.jsonl'))
    discarded = next(json.loads(line)['events'] for line in archive.read_text(encoding='utf-8').splitlines()
                     if json.loads(line).get('version') == 16)
    for name, call in [('money_scene', money), ('restored_food_window', window)]:
        source = payload(call)
        anchor = time()*1000
        restored = restore_clock(source, anchor)
        events = [{'id': f'replay-{i}', 'seq': i+100, 'payload': row}
                  for i, row in enumerate(restored['input_output'])]
        if name == 'restored_food_window':
            # Source archive clocks are absolute; shift by the same replay anchor.
            older = deepcopy(discarded)
            for event in older:
                at = event['payload'].get('at')
                if at is not None:
                    event['payload']['at'] = anchor + at-source['now']
            events = older + events
        snapshot = {'scene': source['scene'], 'events': events,
                    'revisions': [{'at': anchor+source.get('scene_at', 0), 'events': []}],
                    'codes': {p: p for p in source['people']}, 'labels': source['people'],
                    'event_refs': {e['id']: f'E{i+1}' for i, e in enumerate(events)}}
        case = {'name': name, 'source_activity': call['activity_id'], 'source_at': call['at'],
                'historical_event_count': len(source['input_output']), 'available_event_count': len(events), 'replays': []}
        report['scene_cases'].append(case)
        selected_scenes.append((case, snapshot, source['main_scene']))
        captures = []
        try:
            JEVSceneWriter(PrepareOnly()).run('stone', deepcopy(snapshot), source['main_scene'],
                on_request=lambda req: captures.append({'system': build_system(req), 'user': build_user(req)}))
        except RuntimeError as exc:
            if str(exc) != 'offline request preparation; no model called':
                raise
        report['prepared_requests'].append({'case': name, **captures[-1]})
    if args.live:
        _load_local_env()
        store = SkillConfigStore()
        gate_model = build_model_port(store.profile('jev'))
        scene_model = build_model_port(store.profile('person_review'))
        if not getattr(gate_model, 'endpoint', None):
            raise RuntimeError('No configured live model')
        gate_model.max_tokens = 1000
        scene_model.max_tokens = 1400
        for case, data in selected:
            for repeat in range(args.repeats):
                captures = []
                started = monotonic()
                result = VoiceJEV(gate_model, fallback_models=[]).decide_batch('stone', *entry_args(data),
                    on_request=lambda req: captures.append({'system': build_system(req), 'user': build_user(req)}))
                case['replays'].append({'repeat': repeat+1, 'elapsed_ms': round((monotonic()-started)*1000),
                    'result': asdict(result), 'request': captures[-1] if captures else None})
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({'case': case['name'], 'repeat': repeat+1, 'route': result.route,
                                  'control': result.control, 'error': result.model_error}, ensure_ascii=False), flush=True)
        for case, snapshot, main_scene in selected_scenes:
            name = case['name']
            for repeat in range(args.repeats):
                captures = []
                working = deepcopy(snapshot)
                started = monotonic()
                try:
                    scene, raw = JEVSceneWriter(scene_model).run('stone', working, main_scene,
                        on_request=lambda req: captures.append({'system': build_system(req), 'user': build_user(req)}))
                    result = {'scene': scene, 'raw': raw, 'material_window': working['material_window']}
                except Exception as exc:
                    result = {'error': f'{type(exc).__name__}: {exc}'}
                case['replays'].append({'repeat': repeat+1, 'elapsed_ms': round((monotonic()-started)*1000),
                    'result': result, 'request': captures[-1] if captures else None})
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({'case': name, 'repeat': repeat+1, 'result': result.get('scene', result.get('error'))}, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
