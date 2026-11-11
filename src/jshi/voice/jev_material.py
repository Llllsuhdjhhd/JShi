"""Shared, immutable model materials for both JEV1 execution modes."""
from __future__ import annotations

from copy import deepcopy
import json
import math


SUPPORT_BANDS = ('强不支持', '不支持', '区分不明显', '支持', '强支持')
# Short replies use only current speech and general knowledge when the person is tentative.
SHORT_REPLY_LEVELS = frozenset({'确定', '可能'})
TASK_MATERIAL_SCOPE = ('以下仅为任务执行与采用状态，不含工具结果正文。'
                       'waiting_for为待补信息，expires_at为带时区的原任务适用截止；任务待补不等于发言未完，过期不续办。'
                       'result_available表示程序侧有结果，不表示本次已提供其内容；completion_verified仅表示提供了产物核验依据，整项需求是否满足仍需主认知结合正文判断；'
                       '匠石已播的具体说法未与结果正文核对时，保留“已说、依据未核实”，不概括成任务结果已交付。')


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


def build_snapshot(payload, batch, delivery, *, source_ids=None):
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
    events, seen, unidentified, identified, size, correction_size, deferred = [], set(), set(), set(), 0, 0, 0
    name_codes = {name: code for code, name in delivery.get('named_people', {}).items()}
    source_ids = source_ids or {}
    candidates = [(row, source_ids.get((view, i)))
                  for view, rows in (('unwritten', payload['input'].get('unwritten', [])),
                                     ('conversation', payload.get('conversation', [])))
                  for i, row in enumerate(rows)]
    # Reserve new corrections, then the nearest event neighborhood. Filling
    # from oldest first used to retain an old assistant question but drop the
    # recent human exchange that explains the current addressee.
    def priority(row):
        at = row.get('at')
        timed = type(at) in (int, float) and math.isfinite(at)
        return (row.get('kind') != 'understanding_update', not timed, -at if timed else 0)
    candidates.sort(key=lambda item: priority(item[0]))
    for row, input_id in candidates:
        person = row.get('p', row.get('speaker', row.get('who')))
        if isinstance(person, str):
            person = name_codes.get(person, person)
        key = (row.get('at'), row.get('text'), json_text(person))
        if row.get('at') is None:
            key = (*key, row.get('ref', row.get('evidence_ref', len(events))))
        identity_key = (input_id, row.get('text'))
        if ((input_id and (identity_key in identified or key in unidentified))
                or not input_id and key in seen):
            continue
        seen.add(key)
        if input_id:
            identified.add(identity_key)
        else:
            unidentified.add(key)
        cost = len(json_text(row))
        correction = row.get('kind') == 'understanding_update'
        # Keep room for the human exchange as well as accumulated corrections.
        if size + cost > 8000 or correction and correction_size + cost > 2000:
            deferred += 1
            continue
        size += cost
        if correction:
            correction_size += cost
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
                          'now': payload.get('now'), 'time_basis': payload['input'].get('time_basis', ''),
                          'event_time_unit': payload['input'].get('event_time_unit')},
                'evidence': evidence, 'environment': environment,
                'people': deepcopy(delivery.get('named_people', {})),
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
        now, at = delivery.get('now'), origin.get('at_ms')
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
            if str(hang.meta.get('invalidated') or '').strip().lower() in {'1', 'true', 'yes'}:
                continue
            rows.append((owner, hang))
    rows.sort(key=lambda pair: pair[1].updated_at, reverse=True)
    result = []
    for owner, hang in rows[:6]:
        work = service.work_index.for_task(hang.task_id)
        # A wrapper summary can describe progress or failure. Neither it nor
        # "notified" proves a successful result or delivery to the listener.
        terminal = next((f.result for f in reversed(hang.feedback)
                         if str(f.kind) == 'result' and f.result is not None), None)
        outcome = str(terminal.status) if terminal is not None else ''
        state = ('已取消' if hang.status == 'cancelled' else
                 '执行已结束' if terminal is not None else
                 '执行中' if hang.status == 'open' else '执行状态未核实')
        handling = [w.get('handling', {}).get(hang.task_id, {}) for w in work]
        adopted = any(h.get('disposition') == 'answered' and h.get('evidence') for h in handling)
        deferred = any(h.get('disposition') == 'deferred' for h in handling)
        delivery = ('已记录答复采用；是否实际送达须核对交付记录' if adopted else
                    '暂存待交付' if deferred else '交付情况未核实')
        result.append({'ref': f'Q{len(result)+1}', 'owner': code_of(owner) if code_of else '当前已绑定人物',
                       'purpose': hang.need[:200], 'state': state,
                       'updated_at': hang.updated_at.timestamp(),
                       'result': {'ok': '执行结束', 'partial': '部分完成', 'failed': '失败',
                                  'aborted': '中止', 'rejected': '拒绝',
                                  'budget_exceeded': '预算不足'}.get(outcome, '尚无终态结果'),
                       'result_available': terminal is not None and outcome in {'ok', 'partial'},
                       'completion_verified': any(r.get('kind') == 'verification' and r.get('verdict') == 'passed' for r in terminal.execution) if terminal else False,
                       'entered_main': hang.delivered_at is not None,
                       'delivery': delivery,
                       'closed': bool(work) and all(w.get('closed') for w in work)})
    intake_store = getattr(service, 'intake_store', None)
    if intake_store is not None:
        for owner in sorted(owners):
            for intake in intake_store.list_for(subject_id, owner):
                if intake.status not in {'failed', 'waiting', 'expired'}:
                    continue
                works = service.work_index.for_task(intake.intake_id)
                if works and all(w.get('closed') for w in works):
                    continue
                result.append({'owner': code_of(owner) if code_of else '当前已绑定人物',
                    'purpose': intake.need[:200], 'state': {'waiting':'待补信息，尚未执行', 'expired':'适用时段已过，不再续办'}.get(intake.status, '策划未完成，尚未执行'),
                    'waiting_for': list(dict.fromkeys(q for w in works for q in w.get('waiting_for', ()))),
                    'expires_at': next((w.get('expires_at') for w in works if w.get('expires_at')), ''),
                    'updated_at': intake.updated_at.timestamp(), 'result': intake.plan_error[:160],
                    'result_available': False, 'completion_verified': False,
                    'entered_main': bool(intake.delivered_at), 'delivery': '按交付记录核对', 'closed': False})
    result.sort(key=lambda r: r['updated_at'], reverse=True)
    return [{**row, 'ref': f'Q{i}'} for i, row in enumerate(result[:6], 1)]


def scene_task_context(process, subject_id, events, codes):
    """Use only confirmed people in the scene writer's actually selected events."""
    from jshi.core.envelope import SpeakerEvidence
    speakers = []
    for event in events:
        identity = event.get('identity') or {}
        certain = identity.get('certainty') in {'confirmed', '确定'}
        person = identity.get('person') if certain else event.get('p')
        owner = codes.get(person)
        if owner and (certain or event.get('status') == 'recognized'):
            speakers.append(SpeakerEvidence(track_id='', object_id=owner, status='recognized'))
    reverse = {owner: code for code, owner in codes.items()}
    return task_context(process, subject_id, speakers, code_of=reverse.get)


def entry_text(snapshot):
    from jshi.core.scene_context import scene_cautions
    updates = [r for r in snapshot['events'] if r.get('kind') == 'understanding_update']
    events = [r for r in snapshot['events'] if r.get('kind') != 'understanding_update']
    return blocks([('当前输入', snapshot['lines']), ('现场认识更新·有范围的解释', updates),
                   ('当前现场·JEV2已编排', snapshot['scene']), ('有效观察', snapshot['environment']),
                   ('尚未编排的新事件', events), ('本批材料用法', scene_cautions(snapshot['lines'], snapshot['environment'])),
                   ('当前状态', snapshot['state']),
                   ('相识情况', snapshot.get('acquaintance', {})), ('人物证据', snapshot['evidence']),
                   ('媒体关联', snapshot['media']),
                   ('任务材料范围', TASK_MATERIAL_SCOPE if snapshot['task_context'] else None),
                   ('相关任务', snapshot['task_context']),
                   ('材料范围', snapshot['scope'])])


def support_for_main(snapshot):
    """Small provenance delta, not another copy of the scene or classifier table."""
    result = []
    for line in snapshot.get('lines', []):
        if line.get('source') in {'web_text', 'visual_environment'}:
            continue  # No missing voice evidence in an explicitly non-audio input.
        evidence = next((r for r in snapshot['evidence'] if r.get('ref') == line.get('evidence_ref')), {})
        if evidence.get('same_as'):
            evidence = next((r for r in snapshot['evidence'] if r.get('ref') == evidence['same_as']), {})
        voice = evidence.get('voice', {})
        notes = []
        basis = voice.get('basis', '')
        if isinstance(basis, str) and basis:
            notes.append(basis.replace('本句候选比较见candidates；', ''))
        if line.get('short_voice_hint'):
            notes.append(line['short_voice_hint'])
        if line.get('continuity'):
            notes.append('声音连续性只支持同一声音，不确认姓名')
        if line.get('overlap'):
            notes.append('本句含叠音，不据转写补造清楚的意图')
        if line.get('person_scope') == 'unaligned_cloud_span':
            notes.append('整句声音未可靠分人，不能把局部人物扩展到整句')
        if line.get('asr_previous'):
            notes.append('同一音频此前转写：'+ ' / '.join(line['asr_previous'])+
                         '；与当前文字是不同识别版本，不是重复发言；后到不保证更准确，意思冲突仍须核对')
        delay = (line.get('audio_delivery') or {}).get('recognition_delay_seconds')
        if type(delay) is int and delay >= 1:
            notes.append(f'本次转写在对应音频末尾实际收包约{delay}秒后才到达；这是识别处理间隔，不是本句时长。'
                         '间隔本身不证明内容过时；核对原事件及其后是否已回应或转向，未答的明确请求仍须处理')
        if notes:
            result.append({'n': line['i'], 'notes': list(dict.fromkeys(notes))})
    recent = _recent_event_rows_for_main(snapshot)
    if recent and snapshot.get('lines'):
        first = snapshot['lines'][0]['i']
        support = next((row for row in result if row['n'] == first), None)
        if support is None:
            support = {'n': first, 'notes': []}
            result.append(support)
        support['recent_events'] = recent
    return tuple(result)


def recent_context_for_main(snapshot, *, limit=6, cap=1200):
    return '\n'.join(_recent_event_rows_for_main(snapshot, limit=limit, cap=cap))


def _recent_event_rows_for_main(snapshot, *, limit=6, cap=1200):
    """Preserve nearby original exchanges that a stale scene may not yet cover."""
    current = {(row.get('p'), row.get('text'), row['at']) for row in snapshot.get('lines', [])
               if type(row.get('at')) in (int, float) and math.isfinite(row['at'])}
    people = snapshot.get('people', {})
    state = snapshot.get('state', {})
    unit = state.get('event_time_unit')
    if not unit and '整数毫秒' in state.get('time_basis', ''):
        unit = 'milliseconds'
    result, size = [], 0
    for row in reversed(snapshot.get('events', [])):
        if row.get('kind') == 'understanding_update' or not row.get('text'):
            continue  # Corrections already have their own bounded shared projection.
        person = row.get('p', row.get('who', row.get('speaker', '归属未明')))
        if not isinstance(person, str) or (person, row['text'], row.get('at')) in current:
            continue
        label = people.get(person, person) or '归属未明'
        if label == person:
            import re
            if re.fullmatch(r'[PS]\d+', person):
                label = f'入口临时对象{person}（仅该入口内的代号）'
        at = row.get('at')
        known = type(at) in (int, float) and math.isfinite(at) and unit in {'seconds', 'milliseconds'}
        if known and at > 0:
            continue
        age = abs(at) / (1000 if unit == 'milliseconds' else 1) if known else None
        when = f'入口判断前约{round(age)}秒' if known else '事件时间未知'
        visual = row.get('source') == 'visual_environment' or bool(row.get('environment'))
        if visual:
            label = '现场观察（非人物发言）'
        basis = row.get('basis') or ('视觉观察' if visual else '事件原文，交付状态未提供')
        identity = (row.get('identity') or {}).get('certainty') or row.get('status') or '未提供'
        item = f'{when}，{label}（{basis}；人物可靠性：{identity}）：{row["text"]}'
        if size + len(item) > cap:
            continue  # Keep whole utterances, including qualifications.
        result.insert(0, item)
        size += len(item)
        if len(result) >= limit:
            break
    return tuple(result)


def preparation_text(snapshot, entry, *, main_only=False):
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
    updates = [r for r in snapshot['events'] if r.get('kind') == 'understanding_update']
    events = [r for r in snapshot['events'] if r.get('kind') != 'understanding_update']
    return blocks([('可用称呼', addresses), ('本次处理原话', lines),
                   ('现场认识更新·有范围的解释', updates),
                   ('当前现场与新事件', {'scene': snapshot['scene'], 'events': events,
                    **({'answer_handling': snapshot['state']['answer_handling']}
                       if snapshot.get('state', {}).get('answer_handling') else {}),
                    **({'utterance_context': snapshot['state']['utterance_context']}
                       if snapshot.get('state', {}).get('utterance_context') else {})}),
                   ('入口结论', preparation_entry),
                   ('处理许可与材料可用性', {**({} if main_only else {'short_chat': short}),
                    'waiting': snapshot['waiting'] if direct else {'allowed': False}, 'activity_state': snapshot.get('state', {}).get('state', 'unknown'),
                    **snapshot['availability']}),
                   ('有效观察', snapshot['environment']),
                   ('任务材料范围', TASK_MATERIAL_SCOPE if snapshot['task_context'] else None),
                   ('相关任务', snapshot['task_context']),
                   ('材料范围', snapshot['scope'])])
