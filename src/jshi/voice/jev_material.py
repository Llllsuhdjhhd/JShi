"""Shared, immutable model materials for both JEV1 execution modes."""
from __future__ import annotations

from copy import deepcopy
import json
import math


SUPPORT_BANDS = ('强不支持', '不支持', '区分不明显', '支持', '强支持')
# Short replies use only current speech and general knowledge when the person is tentative.
SHORT_REPLY_LEVELS = frozenset({'确定', '可能'})


def support_evidence(candidate):
    """Only validated, version-matched boundaries may turn a score into a band."""
    score = candidate.get('score', candidate.get('classification_score', candidate.get('similarity')))
    calibration = candidate.get('support_calibration') or {}
    edges = calibration.get('boundaries', [])
    valid = (calibration.get('validated') is True and candidate.get('model_version')
             and calibration.get('model_version') == candidate['model_version']
             and calibration.get('source') == candidate.get('source')
             and isinstance(edges, list) and len(edges) == 4
             and all(type(v) in (int, float) and math.isfinite(v) for v in edges)
             and all(a < b for a, b in zip(edges, edges[1:])))
    numeric = type(score) in (int, float) and math.isfinite(score)
    support = (SUPPORT_BANDS[sum(score >= v for v in edges)] if valid and numeric
               else '未分档' if numeric else '无有效证据')
    result = {'p': candidate.get('who', candidate.get('p', 'unknown')),
              'source': candidate.get('source', 'voice'), 'support': support}
    if numeric and candidate.get('source') in {'voiceprint', 'voiceprint_match', 'repeated_short_audio'}:
        result['similarity'] = round(score, 3)  # Similarity, never identity probability.
    if type(candidate.get('verified')) is bool:
        result['validation'] = 'cross_session' if candidate['verified'] is True else 'limited'
    elif 'validation' in candidate:
        result['validation'] = candidate['validation']
    for key in ('names', 'applicability', 'sample_limited', 'stale_labels'):
        if key in candidate:
            result[key] = deepcopy(candidate[key])
    return result


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def blocks(parts):
    return '\n\n'.join(f'【{title}】\n{value if isinstance(value, str) else json_text(value)}'
                        for title, value in parts if value not in (None, '', [], {}))


def build_snapshot(payload, batch, delivery):
    """Payload has already been compacted ONCE; both stages reuse its aliases."""
    lines, evidence, shared, first_of = [], [], {}, {}
    for row, original in zip(payload['input']['lines'], batch):
        lines.append({k: deepcopy(v) for k, v in row.items()
                      if k not in {'voice', 'candidates', 'voice_ranking', 'joint_evidence'}})
        voice = row.get('voice', {})
        candidates = original.get('candidates', [])
        for c in candidates:
            if c.get('source') == 'scene':
                shared.setdefault(c.get('who', c.get('p')), support_evidence(c))
        brief = {k: voice[k] for k in ('pick', 'strength', 'basis', 'measurement') if k in voice}
        brief.update({k: round(voice[k], 3) for k in ('score', 'threshold', 'gap', 'margin')
                      if type(voice.get(k)) in (int, float) and math.isfinite(voice[k])})
        item = {'ref': row['evidence_ref'], 'voice': brief,
                'candidates': [support_evidence(c) for c in candidates if c.get('source') != 'scene']}
        joint = row.get('joint_evidence', {})
        if joint:
            item['joint'] = {k: joint[k] for k in ('state', 'voice_reliable', 'mlp_high', 'mlp_priority') if k in joint}
            for kind in ('voice', 'mlp'):
                if joint.get(kind, {}).get('p'):
                    item['joint'][kind] = {'p': joint[kind]['p']}
        key = json_text({k: v for k, v in item.items() if k != 'ref'})
        if key in first_of:
            item = {'ref': item['ref'], 'same_as': first_of[key]}
        else:
            first_of[key] = item['ref']
        evidence.append(item)
    if shared:
        evidence.insert(0, {'scope': 'batch', 'basis': '片场提到的人物，各句共用；不是本句声音证据',
                            'candidates': list(shared.values())})
    # Keep each real event once. Stable E references win over local C references.
    events, seen, size, deferred = [], set(), 0, 0
    name_codes = {name: code for code, name in delivery.get('named_people', {}).items()}
    for row in [*payload['input'].get('unwritten', []), *payload.get('conversation', [])]:
        person = row.get('p', row.get('speaker', row.get('who')))
        if isinstance(person, str):
            person = name_codes.get(person, person)
        key = (row.get('at'), row.get('text'), json_text(person))
        if row.get('at') is None:
            key = (*key, row.get('ref', row.get('evidence_ref', len(events))))
        if key in seen:
            continue
        seen.add(key)
        cost = len(json_text(row))
        if size + cost > 8000:
            deferred += 1
            continue
        size += cost
        events.append(deepcopy(row))
    # Store events and dialogue are two views, not two chronological blocks.
    # Otherwise an old assistant question appended from dialogue appears after
    # the latest human-to-human question and invites a spurious continuation.
    events.sort(key=lambda row: (not (type(row.get('at')) in (int,float) and math.isfinite(row['at'])),
                                row['at'] if type(row.get('at')) in (int,float) and math.isfinite(row['at']) else 0))
    short = deepcopy(payload.get('short_chat', {'allowed': False}))
    waiting = deepcopy(payload.get('waiting', {'allowed': False}))
    if short.get('allowed') is not True:
        short = {'allowed': False}
    if waiting.get('allowed') is not True:
        waiting = {'allowed': False}
    envelope = payload['input'].get('envelope', {})
    availability = {'memory': '按主认知既有召回能力取得；此处未预先召回',
                    'image_available': any(p.get('kind') == 'image' for p in (delivery.get('envelope') or {}).get('parts', []))}
    environment = payload.get('environment') or envelope.get('environment')
    if isinstance(environment, dict) and environment.get('image_available') is False:
        availability['image_available'] = False
    snapshot = {'lines': lines, 'scene': payload.get('scene', ''), 'events': events,
                'state': {**payload.get('playback', {}), 'names': payload['input'].get('names', ['匠石']),
                          'now': payload.get('now'), 'time_basis': payload['input'].get('time_basis', '')},
                'evidence': evidence, 'environment': environment,
                'acquaintance': deepcopy(delivery.get('acquaintance', {})),
                'media': envelope.get('parts', []), 'availability': availability,
                'short_chat': short, 'waiting': waiting,
                'task_context': deepcopy(payload.get('task_context', [])),
                'scope': deepcopy(delivery.get('jev_material_scope', {}))}
    if deferred:
        snapshot['scope']['unprovided_events'] = deferred
        snapshot['scope']['note'] = '超预算事件仍保留程序队列，未据此声称已经理解'
    if payload.get('utterance_context'):
        snapshot['state']['utterance_context'] = deepcopy(payload['utterance_context'])
    if delivery.get('answer_handling'):
        snapshot['state']['answer_handling'] = delivery['answer_handling']
    if delivery.get('interaction_origin'):
        origin = delivery['interaction_origin']
        snapshot['state']['interaction_origin'] = {k: origin[k] for k in ('text', 'basis') if k in origin}
        now, at = payload.get('now'), origin.get('at_ms')
        if type(now) in (int, float) and type(at) in (int, float):
            snapshot['state']['interaction_origin']['age_seconds'] = round(max(0, now-at)/1000)
    return snapshot


def task_context(process, subject_id, speakers, *, code_of=None):
    """Read only tasks of actually bound/recognized participants, never a guessed owner."""
    service = getattr(process, 'tool_service', None)
    if service is None or not hasattr(service, 'hang_store'):
        return []
    owners = {s.object_id for s in speakers if s.object_id and
              (s.method == 'user_selected' or s.status == 'recognized')}
    rows = []
    for owner in sorted(owners):
        for hang in service.hang_store.list_for(subject_id, owner):
            rows.append((owner, hang))
    rows.sort(key=lambda pair: pair[1].updated_at, reverse=True)
    result = []
    for owner, hang in rows[:6]:
        work = service.work_index.for_task(hang.task_id)
        result.append({'ref': f'Q{len(result)+1}', 'owner': code_of(owner) if code_of else '当前已绑定人物',
                       'purpose': hang.need, 'state': hang.status,
                       'updated_at': hang.updated_at.timestamp(),
                       'result_available': bool(hang.summary),
                       'entered_main': hang.delivered_at is not None,
                       'delivered_to_user': any(bool(w.get('delivery_evidence')) for w in work),
                       'closed': bool(work) and all(w.get('closed') for w in work)})
    return result


def entry_text(snapshot):
    return blocks([('当前输入', snapshot['lines']), ('当前现场·JEV2已编排', snapshot['scene']),
                   ('尚未编排的新事件', snapshot['events']), ('当前状态', snapshot['state']),
                   ('相识情况', snapshot.get('acquaintance', {})), ('人物证据', snapshot['evidence']), ('有效观察', snapshot['environment']),
                   ('媒体关联', snapshot['media']), ('相关任务', snapshot['task_context']),
                   ('材料范围', snapshot['scope'])])


def preparation_text(snapshot, entry):
    selected = set(entry['process_refs'])
    # Keep pending participants for continuation safety, but do not send unrelated
    # identity decisions. Full entry and raw evidence remain with the caller.
    relevant = selected | set(entry['pending_refs'])
    preparation_entry = {**entry, 'items': [
        item for item in entry['items'] if f'I{item["i"]}' in relevant]}
    # No voice evidence, candidates or initial guesses cross the identity boundary.
    lines = [{k: v for k, v in row.items() if k not in
              {'p', 'name', 'continuity', 'recent_confirmed', 'identity', 'binding', 'short_voice_hint'}}
             for row in snapshot['lines'] if row['evidence_ref'] in selected]
    addresses = [{'person': item['person'], 'name': row.get('name', '')}
                 for item in preparation_entry['items'] for row in snapshot['lines']
                 if row['evidence_ref'] == f"I{item['i']}" and item.get('certainty') == 'confirmed'
                 and row.get('p') == item['person'] and row.get('name')]
    direct = entry.get('interaction') == 'direct'
    short = snapshot['short_chat'] if direct else {'allowed': False}
    if short.get('person') and any(item.get('certainty') != 'confirmed' for item in preparation_entry['items']
                                   if f'I{item["i"]}' in selected):
        short = {**{k: v for k, v in short.items() if k != 'person'},
                 'person': None, 'reason': '人物暂定，仅可用当前材料或通用知识短答，不用人物材料'}
    return blocks([('可用称呼', addresses), ('入口结论', preparation_entry), ('本次处理原话', lines),
                   ('当前现场与新事件', {'scene': snapshot['scene'], 'events': snapshot['events'],
                    **({'answer_handling': snapshot['state']['answer_handling']}
                       if snapshot.get('state', {}).get('answer_handling') else {}),
                    **({'utterance_context': snapshot['state']['utterance_context']}
                       if snapshot.get('state', {}).get('utterance_context') else {})}),
                   ('处理许可与材料可用性', {'short_chat': short,
                    'waiting': snapshot['waiting'] if direct else {'allowed': False}, 'activity_state': snapshot.get('state', {}).get('state', 'unknown'),
                    **snapshot['availability']}),
                   ('有效观察', snapshot['environment']), ('相关任务', snapshot['task_context']),
                   ('材料范围', snapshot['scope'])])
