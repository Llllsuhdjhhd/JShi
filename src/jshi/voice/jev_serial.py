"""JEV1_1 / JEV1_2, with an equivalent single-call combined protocol."""
from __future__ import annotations

from dataclasses import replace
from copy import copy
import json
import re
from time import monotonic

from jshi.core import SubjectState
from jshi.models import ModelRequest
from jshi.skill.base import parse_json_object
from .jev_material import build_snapshot, entry_text, preparation_text, blocks
from .jev_prompts import ENTRY_INSTRUCTION, PREPARATION_INSTRUCTION
from .jev_errors import JEVStaleError, failure_details


def object_schema(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def text_schema(limit, *, minimum=0):
    return {'type': 'string', 'minLength': minimum, 'maxLength': limit}


def preparation_schema():
    return object_schema({'route': {'enum': ['self_reply', 'main']},
                          'level': {'type': 'integer', 'enum': [1, 2, 3]},
                          'recall_memory': {'type': 'boolean'}, 'main_image': {'enum': ['omit', 'include']},
                          'reply': text_schema(160), 'waiting_reply': text_schema(16),
                          'reason': text_schema(100, minimum=1)})


def entry_schema(lines, refs):
    people = sorted({'unknown', *[r.get('p', 'unknown') for r in lines if r.get('identity') != 'unresolved'],
                     *[c['p'] for r in lines for c in r.get('candidates', []) if c.get('source') != 'temporary_voice']})
    item = object_schema({'i': {'type': 'integer', 'enum': [r['i'] for r in lines]},
                          'person': {'enum': people}, 'certainty': {'enum': ['confirmed', 'tentative', 'unknown']},
                          'why': text_schema(80, minimum=1)})
    current = [r['evidence_ref'] for r in lines]
    def references(values, minimum=0, maximum=None):
        return {'type': 'array', 'items': {'type': 'string', 'enum': values}, 'uniqueItems': True,
                'minItems': minimum, 'maxItems': maximum if maximum is not None else len(values)}
    return object_schema({'items': {'type': 'array', 'items': item, 'minItems': len(lines), 'maxItems': len(lines)},
                          'interaction': {'enum': ['direct', 'uncertain', 'none']},
                          'utterance_complete': {'enum': ['complete', 'incomplete', 'uncertain']},
                          'control': {'enum': ['keep', 'stop', 'redirect']},
                          'route': {'enum': ['background', 'wait', 'prepare']},
                          'process_refs': references(current), 'pending_refs': references(current),
                          'decision_reason': text_schema(120, minimum=1),
                          'decision_refs': references(sorted(refs), 1, 6)})


def validate(value, schema, path='$'):
    """Validate the small exact schema used here, including booleans vs integers."""
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try:
                validate(value, option, path)
                return
            except ValueError:
                pass
        raise ValueError('JEV输出类型不符合Schema')
    kind = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool, 'null': type(None)}
    if kind and type(value) is not types[kind]:
        raise ValueError('JEV输出类型不符合Schema')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('JEV输出枚举无效')
    if kind == 'object':
        if set(value) - set(schema['properties']) or set(schema.get('required', [])) - set(value):
            missing = sorted(set(schema.get('required', [])) - set(value))
            extra = sorted(set(value) - set(schema['properties']))
            raise ValueError(f'JEV输出字段缺失或多余：{path} missing={missing} extra={extra}')
        for key in value:
            validate(value[key], schema['properties'][key], f'{path}.{key}')
    elif kind in {'array', 'string'}:
        low, high = ('minItems', 'maxItems') if kind == 'array' else ('minLength', 'maxLength')
        if not schema.get(low, 0) <= len(value) <= schema.get(high, float('inf')):
            raise ValueError('JEV输出长度无效')
        if kind == 'array':
            if schema.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
                raise ValueError('JEV引用重复')
            for index, v in enumerate(value):
                validate(v, schema['items'], f'{path}[{index}]')
        elif schema.get('minLength', 0) and not value.strip():
            raise ValueError('JEV依据为空')


def combined_instruction():
    # One output contract, not two complete instructions pasted together.
    def section(text, name, end):
        return text.split(f'【{name}】\n', 1)[1].split(f'【{end}】', 1)[0].strip()
    return ('【用途】\n你是匠石的JEV1合并判断。先完成入口判断，仅在route=prepare时完成回应准备。'
            '人物、交往和控制只判断一次；不调用工具、不修改档案。\n\n【输入】\n'
            + section(ENTRY_INSTRUCTION, '输入', '目标任务') + '\n'
            + section(PREPARATION_INSTRUCTION, '输入', '目标任务').replace(
                'entry是本批入口结论', 'entry是你在本次请求内先完成的入口结论')
            + '\n\n【目标任务】\n入口判断：\n'
            + section(ENTRY_INSTRUCTION, '目标任务', '输出')
            + '\n\n仅当入口route=prepare时，使用其身份与范围继续回应准备：\n'
            + section(PREPARATION_INSTRUCTION, '目标任务', '输出')
            + '\n\n【输出】\n只输出Schema允许的一个JSON对象。顶层为入口结果；'
              'preparation仅在顶层route=prepare时填写回应准备对象，其余必须为null。'
              '两种route各属于自己的对象，不得互换。所有引用、人物范围及长度服从Schema。')


def decide_serial(gate, subject_id, batch, context, delivery, *, overlap=False,
                  on_request=None, on_entry=None, is_current=None):
    from .jev import BatchDecision
    cooldown_owner = getattr(gate, '_cooldown_owner', gate)
    if gate.mode == 'decision':
        gate = copy(gate)
    stages, raw = [], ''
    current = is_current or (lambda: True)
    accepted = None
    entry = None
    mode = gate.mode if gate.mode in {'combined', 'decision'} else 'split'
    early_stop = None
    used_decision = False
    if mode == 'decision':
        gate._entry_deadline = monotonic() + 2 * gate.budget_s
    if monotonic() < cooldown_owner._unavailable_until:
        return BatchDecision(action='ignore', model_error=gate._unavailable_error,
            stage_records=({'stage': 'cooldown', 'mode': mode, 'status': 'failed',
                            'error_kind': 'cooldown', 'error': gate._unavailable_error},))
    if overlap:
        batch = [{**r, 'who': 'unknown', 'candidates': [], 'overlap': True,
                  'voice_evidence': {'strength': 'unavailable', 'basis': '重叠音频'}}
                 if r.get('overlap', True) else r for r in batch]
    contextual = set(re.findall(r'(?<![A-Za-z0-9_])P\d+(?![A-Za-z0-9_])', delivery.get('jev_scene', ''))) & set(delivery.get('people', {}))
    if contextual:
        batch = [{**r, 'candidates': [*r.get('candidates', []), *[
            {'who': p, 'source': 'scene', 'names': [delivery['people'][p]]} for p in sorted(contextual)
            if p not in {c['who'] for c in r.get('candidates', [])}]]} for r in batch]
    thin, payload, packed = gate._build_material(batch, context, delivery)
    snapshot = build_snapshot(payload, batch, delivery)
    refs = {r['evidence_ref'] for r in thin}
    refs.update(r.get('ref', r.get('evidence_ref')) for r in snapshot['events'])
    refs.update(r.get('ref') for r in snapshot['task_context'])
    refs.update(re.findall(r'(?<![A-Za-z0-9_])E\d+(?![A-Za-z0-9_])', snapshot['scene']))
    refs.discard(None)
    schema = entry_schema(thin, refs)
    if mode == 'combined':
        schema['properties']['preparation'] = {'anyOf': [preparation_schema(), {'type': 'null'}]}
        schema['required'].append('preparation')

    def call(stage, system, user, output):
        nonlocal raw
        raw = ''
        if not current():
            raise JEVStaleError('JEV材料或播放代次已失效')
        request = ModelRequest(purpose=stage, input_text=user, persona_user_text=user,
            subject_state=SubjectState(subject_id, '匠石', stage),
            system_extra=system + '\nJSON Schema：' + json.dumps(output, ensure_ascii=False, separators=(',', ':')),
            input_parts=tuple(p for p in (packed or {}).get('parts', []) if p.get('kind') == 'image')
                if stage in {'voice_jev', 'voice_jev_entry', 'voice_jev_identity'} else ())
        started = monotonic()
        record = {'stage': stage, 'mode': mode, 'status': 'failed'}
        stages.append(record)
        phase = 'request_record'
        try:
            if on_request:
                on_request(request)
            phase = 'model_call'
            response = gate._generate_gate(request)
            record['model'] = response.model
            metadata = getattr(response, 'metadata', {}) or {}
            record['finish_reason'] = metadata.get('finish_reason')
            record['response_format'] = metadata.get('response_format')
            raw = response.text
            phase = 'output_json'
            data = parse_json_object(raw)
            phase = 'output_schema'
            validate(data, output)
            if not current():
                raise JEVStaleError('JEV返回结果已过期')
            record.update(status='completed', raw=raw)
            return data
        except Exception as exc:
            record.update(failure_details(exc, phase), raw=raw)
            raise
        finally:
            record['ms'] = round((monotonic() - started) * 1000, 1)

    def decision_entry():
        nonlocal early_stop, accepted, used_decision
        from jshi.decision.entry_policy import entry_action
        from .jev_prompts import identity_instruction
        fallback = 'unsupported_scope'
        # First release supports one expression only; never infer same person from track.
        bridge = delivery.get('_decision_entry')
        if len(batch) == 1 and not overlap and not any(p.get('kind') not in {'audio', 'text'} for p in snapshot['media']) and not snapshot['environment'] and not snapshot['scope'].get('unprovided_events') and bridge:
            if not current():
                raise JEVStaleError('JEV决策材料已失效')
            state = {'material': entry_text(snapshot)}
            if len(json.dumps(state, ensure_ascii=False)) <= 24000:
                try:
                    result, config = bridge(state, deadline=gate._entry_deadline)
                    fields = entry_action(result, [r['evidence_ref'] for r in thin],
                                          config['entry_thresholds'][config['provider']])
                    fallback = result.reason or 'uncertain_or_conflicting'
                    stages.append({'stage': 'decision_entry', 'mode': mode, 'status': result.status,
                                   'provider': result.provider, 'model': result.model, 'ms': result.elapsed_ms,
                                   'answers': dict(result.answers), 'fallback_reason': fallback if fields is None else ''})
                    if not current():
                        raise JEVStaleError('JEV决策返回已失效')
                    if fields is not None:
                        # Stop promptly without waiting for name recognition. Host callback is the
                        # sole control owner; remember its receipt across identity and fallback.
                        if fields['control'] == 'stop' and on_entry:
                            provisional = BatchDecision(action='stop', control='stop', route='background')
                            receipt = on_entry(provisional)
                            if receipt is False:
                                raise JEVStaleError('停止许可已失效')
                            early_stop = receipt if receipt is not None else True
                            accepted = replace(provisional, control_applied=type(receipt) is int,
                                               applied_epoch=receipt if type(receipt) is int else None)
                        bound = {r['n']: gate._selected_text(r) for r in batch}
                        if all(bound.values()):
                            people = [{'i': n, 'person': p, 'certainty': 'confirmed',
                                       'why': '文字输入由用户选择人物'} for n, p in bound.items()]
                        else:
                            identity_schema = object_schema({'items': schema['properties']['items'],
                                                            'source_conflict': {'type': 'boolean'}})
                            identity = call('voice_jev_identity', identity_instruction(),
                                            entry_text(snapshot) + '\n\n' + blocks([('待核对的入口假设', fields)]), identity_schema)
                            # Validate per-sentence candidates before committing any identity.
                            gate._from_current(identity['items'], batch, '')
                            if identity['source_conflict']:
                                raise ValueError('人物或媒体来源改变交往判断')
                            people = identity['items']
                        assembled = dict(fields, items=people)
                        validate(assembled, schema)
                        used_decision = True
                        if hasattr(bridge, 'report'):
                            bridge.report('decision', '')
                        return assembled
                except JEVStaleError:
                    raise
                except Exception as exc:
                    fallback = type(exc).__name__
            else:
                fallback = 'material_budget'
        elif not bridge:
            fallback = 'host_bridge_unavailable'
        stages.append({'stage': 'decision_fallback', 'mode': mode, 'status': 'fallback',
                       'actual_mode': 'split', 'reason': fallback})
        if hasattr(bridge, 'report'):
            bridge.report('split', fallback)
        value = call('voice_jev_entry', ENTRY_INSTRUCTION, entry_text(snapshot), schema)
        if early_stop is not None:
            value['control'] = 'stop'
            if value['route'] == 'wait':
                value['route'] = 'background'
            value['decision_reason'] = '已执行停止；' + value['decision_reason'][:110]
        return value

    try:
        if mode == 'decision':
            entry = decision_entry()
            preparation = None
        elif mode == 'combined':
            user = entry_text(snapshot) + '\n\n' + blocks([
                ('回应许可与材料可用性', {'short_chat': snapshot['short_chat'],
                 'waiting': snapshot['waiting'], **snapshot['availability']})])
            entry = call('voice_jev', combined_instruction(), user, schema)
            preparation = entry.pop('preparation')
        else:
            entry = call('voice_jev_entry', ENTRY_INSTRUCTION, entry_text(snapshot), schema)
            preparation = None
        accepted = gate._from_current(entry['items'], batch, '')
        bound = {r['n']: gate._selected_text(r) for r in batch if gate._selected_text(r)}
        if bound:
            accepted = replace(accepted, items=tuple(replace(i, speaker_pick=bound[i.n],
                speaker_level='确定', speaker_reason='文字输入由用户选择人物',
                to_jiangshi='yes', relevance='related') if i.n in bound else i for i in accepted.items))
            entry['items'] = [{'i': i.n, 'person': i.speaker_pick,
                'certainty': {'确定': 'confirmed', '可能': 'tentative', '不确定': 'unknown'}[i.speaker_level],
                'why': i.speaker_reason} for i in accepted.items]
            if len(bound) == len(batch) and not used_decision:
                entry['interaction'] = 'direct'
                if entry['control'] != 'stop' and entry['route'] == 'background' and entry['utterance_complete'] != 'incomplete':
                    entry['route'] = 'prepare'
                    entry['process_refs'] = [f'I{r["n"]}' for r in batch]
                    if delivery.get('state') not in {'thinking', 'playing', 'paused', 'queued'}:
                        entry['control'] = 'redirect'
        process, pending = tuple(entry['process_refs']), tuple(entry['pending_refs'])
        route, control = entry['route'], entry['control']
        if (route == 'prepare' and not process or route == 'wait' and not pending
                or route == 'background' and process
                or entry['utterance_complete'] == 'incomplete' and route == 'prepare'
                or route in {'wait', 'background'} and control == 'redirect'
                or control == 'stop' and route == 'wait'
                or route == 'prepare' and entry['interaction'] == 'none' and control != 'stop'):
            raise ValueError('JEV入口范围、完整度或控制矛盾')
        accepted = replace(accepted, interaction=entry['interaction'], control=control,
            action='stop' if control == 'stop' and route != 'prepare' else 'respond' if route == 'prepare' else 'ignore',
            entry_route=route, route='main' if route == 'prepare' else route,
            utterance_complete=entry['utterance_complete'], process_refs=process, pending_refs=pending,
            decision_reason=entry['decision_reason'], decision_refs=tuple(entry['decision_refs']),
            reason=entry['decision_reason'], silent_operation=control == 'stop' and route == 'prepare')
        # Exclude unfinished inputs from downstream rendering; keep identity judgments intact.
        selected = set(process)
        accepted = replace(accepted, items=tuple(replace(i, to_jiangshi='no', relevance='unrelated')
                           if route == 'prepare' and f'I{i.n}' not in selected else i for i in accepted.items))
        if early_stop is not None:
            accepted = replace(accepted, control_applied=type(early_stop) is int,
                               applied_epoch=early_stop if type(early_stop) is int else None)
        if on_entry and early_stop is None:
            state = on_entry(accepted)
            if state is False:
                raise RuntimeError('JEV入口结果已失效')
            if type(state) is int:
                accepted = replace(accepted, control_applied=True, applied_epoch=state)
        if route != 'prepare':
            if preparation is not None:
                raise ValueError('背景或等待不能附带回应准备')
            return replace(accepted, stage_records=tuple(stages), model_raw=raw)
        if mode != 'combined':
            preparation = call('voice_jev_prepare', PREPARATION_INSTRUCTION,
                               preparation_text(snapshot, entry), preparation_schema())
        if not isinstance(preparation, dict):
            raise ValueError('合并判断缺少回应准备')
        hint = {'level': preparation['level'], 'needs': [], 'recall_memory': preparation['recall_memory'],
                'main_image': preparation['main_image']}
        if not snapshot['availability']['image_available']:
            hint['main_image'] = 'omit'
        short, waiting = snapshot['short_chat'], snapshot['waiting']
        selected_items = [i for i in accepted.items if f'I{i.n}' in selected]
        direct_allowed = (short.get('allowed') is True and entry['interaction'] == 'direct'
            and control == 'redirect' and not overlap and not pending
            and all(r.get('source') != 'visual_environment' for r in batch)
            and all(i.speaker_level == '确定' and i.speaker_pick == short.get('person_code')
                    for i in selected_items) and bool(selected_items)
            and hint == {'level': 3, 'needs': [], 'recall_memory': False, 'main_image': 'omit'})
        direct = preparation['reply'].strip() if preparation['route'] == 'self_reply' and direct_allowed else ''
        route = 'self_reply' if direct else 'main'
        brief = ''
        budget = waiting.get('levels', {}).get(f'L{hint["level"]}', {})
        if (route == 'main' and waiting.get('allowed') is True and control == 'redirect'
                and entry['interaction'] == 'direct' and not pending and not overlap
                and budget.get('expected_seconds', 0) >= 2
                and len(preparation['waiting_reply'].strip()) <= min(16, budget.get('max_chars', 0))):
            brief = preparation['waiting_reply'].strip()
        if accepted.silent_operation:
            route, direct, brief = 'main', '', ''
            hint['level'] = 1
        return replace(accepted, route=route, direct_reply=direct, waiting_reply=brief,
                       main_prompt_hint=hint, needs_main_review=route == 'main',
                       stage_records=tuple(stages), model_raw=raw)
    except Exception as exc:
        status = getattr(exc, 'status', None) or getattr(exc, 'code', None)
        if status in {401, 402, 403}:
            cooldown_owner._unavailable_until = monotonic() + 60
            cooldown_owner._unavailable_error = f'{type(exc).__name__}: HTTP {status}'
        details = failure_details(exc, 'routing')
        if not stages or stages[-1]['status'] != 'failed':
            stages.append(dict(stage='routing', mode=mode, status='failed', **details))
        message = details['error']
        if accepted is not None:
            # Keep valid identity and already-applied controls; a failed preparation never speaks.
            return replace(accepted, action='stop' if accepted.control == 'stop' else 'ignore',
                route='background', direct_reply='', waiting_reply='', main_prompt_hint=None,
                routing_error=message, model_error=message, stage_records=tuple(stages), model_raw=raw)
        action, reason = gate._unavailable_hold(delivery, batch_reason='JEV1_1未能判断')
        items = gate._items_for(batch, 'no', reason)
        items = tuple(replace(i, speaker_pick=gate._selected_text(r), speaker_level='确定',
                              speaker_reason='文字输入由用户选择人物') if gate._selected_text(r) else i
                      for i, r in zip(items, batch))
        return BatchDecision(action=action, reason=reason, items=items, route='background', control='keep',
                             model_error=message, stage_records=tuple(stages), model_raw=raw)
