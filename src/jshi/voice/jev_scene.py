"""Durable JEV scene and unprocessed events, separate from cognition's scene."""
from __future__ import annotations

from copy import deepcopy
import json
import hashlib
import math
import os
import re
from pathlib import Path
from threading import RLock
from time import time, monotonic

from jshi.core import SubjectState
from jshi.models import ModelRequest
from jshi.skill.base import parse_json_object
from .jev_prompts import SCENE_INSTRUCTION


def recent_episode(rows, cutoff_ms):
    """Select the continuous conversation before this input, without deleting history."""
    seconds = float(os.getenv('JSHI_JEV_CONTEXT_IDLE_SECONDS', '300'))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('JSHI_JEV_CONTEXT_IDLE_SECONDS 必须为正数秒数')
    timed = []
    for row in rows:
        timestamp = (row.get('payload') or row).get('at')
        if type(timestamp) in (int, float) and math.isfinite(timestamp) and 0 < timestamp <= cutoff_ms:
            timed.append((timestamp, row))
    timed.sort(key=lambda item: item[0])
    boundary = cutoff_ms
    start = len(timed)
    for index in range(len(timed) - 1, -1, -1):
        timestamp = timed[index][0]
        if boundary - timestamp > seconds * 1000:
            break
        start = index
        boundary = timestamp
    selected = {id(row) for _, row in timed[start:]}
    return [row for row in rows if id(row) in selected]


class JEVSceneStore:
    def __init__(self, path: Path):
        self.path, self.lock = Path(path), RLock()
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "scene": "", "version": 0, "generation": 0, "sequence": 0,
            "events": [], "covered": [], "codes": {}, "labels": {},
        }

        # Stable event references survive rolling revisions and process restarts.
        refs = self.state.setdefault('event_refs', {})
        old_rows = [r for rev in self.state.get('revisions', []) for r in rev.get('events', [])] + self.state['events']
        for row in old_rows:
            refs.setdefault(row['id'], f'E{len(refs)+1}')

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)
        self.state = state

    def append(self, event_id, payload, *, codes=None, labels=None):
        with self.lock:
            state = deepcopy(self.state)
            fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            previous = state.setdefault('event_fingerprints', {}).get(event_id)
            if event_id in state["covered"]:
                if previous == fingerprint:
                    return
                state['covered'].remove(event_id)
                state['generation'] += 1
            if event_id in state.get('archived_unread', []):
                if previous == fingerprint:
                    return
                state['archived_unread'].remove(event_id)
                state['generation'] += 1
            state['event_fingerprints'][event_id] = fingerprint
            if previous != fingerprint:
                versions = state.setdefault('event_versions', {})
                versions[event_id] = versions.get(event_id, 0) + 1
            existing = next((r for r in state["events"] if r["id"] == event_id), None)
            if existing:
                if existing["payload"] != payload:
                    state["generation"] += 1
                    existing["payload"] = deepcopy(payload)
            else:
                state["sequence"] += 1
                state["events"].append({"id": event_id, "seq": state["sequence"], "queued_at": time()*1000, "payload": deepcopy(payload)})
            state.setdefault('event_refs', {}).setdefault(event_id, f"E{len(state.get('event_refs', {}))+1}")
            if codes is not None:
                state["codes"] = dict(codes)
                state["labels"] = dict(labels or {})
            self._save(state)

    def snapshot(self):
        with self.lock:
            result = deepcopy(self.state)
            candidates, size = [], 0
            for row in reversed(self._history_rows()):
                if (row['generation'] != self.state['generation'] or not row['scene']
                        or row['scene'] == self.state['scene']
                        or any(self.state['codes'].get(code) != oid for code, oid in row['codes'].items())):
                    continue
                candidate = {'h': f'H{len(candidates)+1}', 'scene': row['scene'], 'at': row['at']}
                if len(candidates) >= 2 or size + len(row['scene']) > 200:
                    continue
                candidates.append(candidate)
                size += len(row['scene'])
                result.setdefault('history_lookup', {})[candidate['h']] = self._history_key(row)
            result['history_candidates'] = candidates
            return result

    @staticmethod
    def _history_key(row):
        return [row['version'], row['generation'], row['at'], row['reason']]

    def _history_rows(self):
        """Age/character bounds apply to storage as well as model-facing previews."""
        path = self.path.with_suffix('.history.jsonl')
        if not path.exists():
            return []
        with path.open(encoding='utf-8') as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        kept = self._bounded_history(rows)
        if kept != rows:
            self._write_history(kept)
        return kept

    @staticmethod
    def _bounded_history(rows):
        age = float(os.getenv('JSHI_JEV_HISTORY_MAX_AGE_SECONDS', '300'))
        limit = int(os.getenv('JSHI_JEV_HISTORY_MAX_CHARS', '500'))
        if not math.isfinite(age) or age <= 0 or limit <= 0:
            raise ValueError('JEV片场历史保存时间和字符上限必须为正数')
        now_ms, kept, size = time()*1000, [], 0
        for row in reversed(rows):
            if not 0 <= now_ms-row['at'] <= age*1000:
                continue
            # This is a short scene cache, not an archive of utterances.
            row = {key: row[key] for key in ('version', 'generation', 'at', 'scene', 'codes', 'reason')}
            cost = len(row['scene'])
            if cost and len(kept) < 6 and size + cost <= limit:
                kept.append(row)
                size += cost
        return list(reversed(kept))

    def _write_history(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        path = self.path.with_suffix('.history.jsonl')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in rows), encoding='utf-8')
        temporary.replace(path)

    def _archive(self, revision):
        """Keep only short scene summaries outside the rolling revision cache."""
        rows = self._history_rows()
        rows.append(revision)
        self._write_history(self._bounded_history(rows))

    def reference(self, version, *, cutoff_ms=None):
        """Explicit historical lookup; never changes the active scene or replays speech.

        References from before a manual correction are deliberately ineligible.
        Original dialogue logs are independent of this short summary cache.
        """
        cutoff_ms = time()*1000 if cutoff_ms is None else cutoff_ms
        with self.lock:
            found = None
            for row in self._history_rows():
                if (row['version'] == version and row['at'] <= cutoff_ms
                        and row['generation'] == self.state['generation']):
                    found = row
            if found is None:
                return None
            # A historical P code is meaningful only with the original mapping.
            # Do not return a reference when codes have since been rebound.
            if any(self.state['codes'].get(code) != oid for code, oid in found['codes'].items()):
                return None
            return deepcopy(found)

    def material_view(self, cutoff_ms, current_ids=()):
        """Read scene and uncovered events atomically; age never implies coverage."""
        with self.lock:
            scene, version = '', 0
            rows = list(self.state['events'])
            for revision in self.state.get('revisions', []):
                if revision['at'] <= cutoff_ms:
                    scene, version = revision['scene'], revision.get('version', 0)
                else:
                    rows.extend(revision['events'])
            current, seen, visible = set(current_ids), set(), []
            for row in sorted(rows, key=lambda r: r['seq'], reverse=True):
                if row['id'] in current or row['id'] in seen:
                    continue
                at = row['payload'].get('at')
                timestamp = at if type(at) in (int, float) and math.isfinite(at) and at > 0 else row.get('queued_at')
                if timestamp is None or timestamp > cutoff_ms:
                    continue
                seen.add(row['id'])
                result = deepcopy(row['payload'])
                result['ref'] = self.state['event_refs'].get(row['id'], f"E{row['seq']}")
                for change in result.get('corrected_events', []):
                    change['ref'] = self.state['event_refs'].get(change['ref'], 'unknown')
                visible.append(result)
            return {'scene': scene, 'events': list(reversed(visible)), 'version': version,
                    'generation': self.state['generation'],
                    'archived_unread_count': len(self.state.get('archived_unread', []))}

    def view(self, cutoff_ms, current_ids=()):
        material = self.material_view(cutoff_ms, current_ids)
        return material['scene'], material['events']


    def correct(self, payload, *, codes=None, labels=None):
        """An in-flight writer cannot restore a scene predating manual corrections."""
        with self.lock:
            state = deepcopy(self.state)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not state.get('history_migrated'):
                for index, previous in enumerate(state.get('revisions', [])):
                    self._archive({**previous,
                        'version': previous.get('version', state['version']-len(state['revisions'])+index+1),
                        'generation': state['generation'], 'codes': state['codes'],
                        'labels': state['labels'], 'reason': 'legacy_revision'})
                state['history_migrated'] = True
            self._archive({'version': state['version'], 'generation': state['generation'],
                'at': time()*1000, 'scene': state['scene'], 'events': state['events'],
                'codes': state['codes'], 'labels': state['labels'], 'reason': 'before_correction'})
            state["generation"] += 1
            state["version"] += 1
            # Preserve unrelated activities; old identity claims are now provisional.
            notice = "【人工纠正优先】" + str(payload.get('text', '人物归属已更新'))
            base = state.get('scene_before_correction', state['scene'])
            state['scene_before_correction'] = base
            state["scene"] = notice + "\n【纠正前情境参考，仅相关身份待复核】\n" + base
            corrected = {r['ref']: r for r in payload.get('corrected_events', []) if r.get('ref')}
            for event_id in corrected:
                state['event_refs'].setdefault(event_id, f'E{len(state["event_refs"])+1}')
            for row in state['events'] + [r for rev in state.get('revisions', []) for r in rev.get('events', [])]:
                change = corrected.get(row['id'])
                if change:
                    row['payload']['p'] = change.get('person', 'unknown')
                    row['payload']['basis'] = payload.get('basis', 'manual_annotation')
                    row['payload']['judgment'] = {'person': change.get('person', 'unknown'),
                        'certainty': 'unknown' if payload.get('basis') == 'manual_annotation_undo' else 'confirmed',
                        'why': '人工修正本句；仅在指定事件范围生效'}
            state['revisions'] = [*state.get('revisions', []), {
                'scene': state['scene'], 'at': time()*1000, 'events': [],
                'version': state['version'], 'generation': state['generation'] }][-8:]
            self._save(state)
            changes = payload.get('corrected_events', [])
            groups = [changes[i:i+6] for i in range(0, len(changes), 6)] or [[]]
            for i, group in enumerate(groups):
                part = {**payload, 'corrected_events': group} if changes else payload
                self.append(f"correction:{state['generation']}:{i}", part, codes=codes, labels=labels)

    def commit(self, snapshot, scene):
        if not isinstance(scene, str) or not scene.strip() or len(scene) > 1000:
            raise ValueError("JEV片场必须为非空正文且不超过1000字符")
        with self.lock:
            if (snapshot["version"], snapshot["generation"]) != (self.state["version"], self.state["generation"]):
                return False
            window = snapshot.get('material_window')
            ids = set(window['model_input_ids']) | set(window.get('archived_input_ids', [])) if window else {r['id'] for r in snapshot['events']}
            consumed = [r for r in snapshot['events'] if r['id'] in ids]
            state = deepcopy(self.state)
            state["scene"] = scene
            state.pop("scene_before_correction", None)
            state["version"] += 1
            archived = set(snapshot.get("material_window", {}).get("archived_input_ids", []))
            if archived:
                # Full skipped records remain available for analysis after the
                # rolling revision cache expires. Failed/stale writes archive nothing.
                with self.path.with_suffix(".archive.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"version": state["version"], "reason": "oversized_single_event",
                        "events": [r for r in snapshot["events"] if r["id"] in archived]}, ensure_ascii=False) + "\n")
            revisions = state.get("revisions", [])
            # Migrate retained pre-archive revisions before the rolling window drops them.
            if not self.state.get('history_migrated'):
                for index, previous in enumerate(revisions):
                    self._archive({**previous, 'version': previous.get('version', self.state['version']-len(revisions)+index+1),
                        'generation': state['generation'], 'codes': snapshot['codes'],
                        'labels': snapshot['labels'], 'reason': 'legacy_revision'})
            revision = {"scene": scene, "at": time()*1000, "events": [r for r in consumed if r["id"] not in archived],
                        'version': state['version'], 'generation': state['generation'],
                        "material_window": snapshot.get("material_window", {}),
                        "covered_versions": {r["id"]: snapshot.get("event_versions", {}).get(r["id"], 1) for r in consumed if r["id"] not in archived}}
            self._archive({**revision,
                'codes': snapshot['codes'], 'labels': snapshot['labels'], 'reason': 'scene_update'})
            discarded = [snapshot.get('history_lookup', {}).get(h) for h in snapshot.get('discard_history', [])]
            if any(discarded):
                # Only references in this exact model snapshot are eligible.
                self._write_history([row for row in self._history_rows()
                                     if self._history_key(row) not in discarded])
                revisions = [row for row in revisions if not any(
                    key and row.get('version') == key[0] and row.get('generation') == key[1]
                    and row['at'] == key[2] for key in discarded)]
            state['history_migrated'] = True
            revisions.append(revision)
            state["revisions"] = revisions[-8:]
            understood = [r['id'] for r in consumed if r['id'] not in archived]
            state['covered'] = list(dict.fromkeys([*state['covered'], *understood]))
            state['archived_unread'] = list(dict.fromkeys([*state.get('archived_unread', []), *archived]))
            state.setdefault('covered_versions', {}).update({
                event_id: state.get('event_versions', {}).get(event_id, 1) for event_id in understood})
            state["events"] = [r for r in state["events"] if r["id"] not in ids]
            self._save(state)
            return True


SCENE_SCHEMA = {"type": "object", "properties": {"scene": {"type": "string", "minLength": 1, "maxLength": 1000}},
                "required": ["scene"], "additionalProperties": False}

RECENT_MATERIAL_CHARS = 3000


def scene_refresh_reason(snapshot, *, idle_seconds, pending_seconds, idle_threshold=15,
                         input_count=6, input_chars=800, max_pending_seconds=30):
    """Fallback refresh when no main turn has triggered a recent scene pass."""
    if not snapshot['events'] or idle_seconds < idle_threshold:
        return ''
    inputs = [r for r in snapshot['events'] if not r['id'].startswith(('played:', 'stopped:'))]
    if len(inputs) >= input_count:
        return 'idle_input_count'
    if sum(len(str(r['payload'].get('text', ''))) for r in inputs) >= input_chars:
        return 'idle_input_chars'
    return 'idle_pending_age' if pending_seconds >= max_pending_seconds else ''


def recent_material(snapshot, main_scene, cap=RECENT_MATERIAL_CHARS):
    """Read a chronological prefix of compact dialogue, with separate evidence limits.

    Unread ordinary events remain pending. A single overlong event is explicitly
    archived intact, so it cannot block the queue forever or be silently clipped.
    """
    from .scene_material import dialogue_row, identity_evidence, encoded_size, recent_scene_blocks
    selected, rows, oversized = [], [], []
    aliases = snapshot.get('event_refs', {})
    def pack(event):
        result = {**dialogue_row(event['payload']), 'ref': aliases.get(event['id'], f"E{event['seq']}")}
        for change in result.get('corrected_events', []):
            change['ref'] = aliases.get(change['ref'], 'unknown')
        return result
    for event in sorted(snapshot['events'], key=lambda r: r['seq']):
        row = pack(event)
        if encoded_size([row]) > cap:
            oversized.append(event['id'])
            continue
        if encoded_size([*rows, row]) > cap:
            break
        rows.append(row)
        selected.append(event)
    selected_ids = {r['id'] for r in selected}
    # Evidence is secondary to words and playback. Uncertain cases are considered first.
    evidence, evidence_chars = [], 0
    for event in sorted(selected, key=lambda r: (r['payload'].get('judgment', {}).get('certainty') in {'确定', 'confirmed'}, r['seq'])):
        item = identity_evidence(event['payload'])
        candidate = {'ref': pack(event)['ref'], **item}
        if item and encoded_size([*evidence, candidate]) <= 1800:
            evidence.append(candidate)
            evidence_chars = encoded_size(evidence)
    reference, omitted_blocks = recent_scene_blocks(main_scene)
    context = []
    for revision in reversed(snapshot.get('revisions', [])):
        for event in reversed(revision.get('events', [])):
            if (event['id'] in selected_ids or event['id'] in {r['id'] for r in snapshot['events']}
                    or pack(event)['ref'] in {r['ref'] for r in context}):
                continue
            row = pack(event)
            if encoded_size(context + [row]) <= 800:
                context.insert(0, row)
            if len(context) >= 4:
                break
        if len(context) >= 4:
            break
    snapshot['identity_evidence'] = evidence
    snapshot['recent_context'] = context
    snapshot['material_window'] = {
        'cap_chars': cap, 'chars': encoded_size(rows),
        'model_input_ids': [r['id'] for r in selected],
        'archived_input_ids': oversized,
        'deferred_input_ids': [r['id'] for r in snapshot['events'] if r['id'] not in selected_ids and r['id'] not in oversized],
        'evidence_chars': evidence_chars, 'main_scene_chars': len(reference),
        'omitted_main_scene_blocks': omitted_blocks,
    }
    return rows, reference


class JEVSceneWriter:
    def __init__(self, model):
        self.model = model
        self._unavailable_until = 0.0

    def run(self, subject_id, snapshot, main_scene, *, on_request=None, history_reference=None):
        if monotonic() < self._unavailable_until:
            from .jev_errors import JEVCooldownError
            raise JEVCooldownError('JEV片场接口计费或认证失败；60秒冷却期间保留待办')
        # Internal long IDs and progress belong to the store, never to the prompt.
        events, reference = recent_material(snapshot, main_scene)
        now_ms = time()*1000
        revisions = snapshot.get('revisions', [])
        payload = {"now": now_ms, "scene": snapshot["scene"],
            'scene_at': revisions[-1]['at'] if revisions else None,
            "input_output": events, "main_scene": reference,
            "identity_evidence": snapshot.get('identity_evidence', []),
            "recent_context": snapshot.get('recent_context', []),
            "window": {"limit": RECENT_MATERIAL_CHARS, "omitted_events": len(snapshot["material_window"]["archived_input_ids"]),
                       "deferred_events": len(snapshot["material_window"].get("deferred_input_ids", [])),
                       "evidence_limit": 1800, "reference_limit": 800,
                       "omitted_main_blocks": snapshot["material_window"]["omitted_main_scene_blocks"]},
            "people": {code: snapshot["labels"].get(oid, code) for code, oid in snapshot["codes"].items()}}
        if snapshot.get('history_candidates'):
            payload['history_candidates'] = snapshot['history_candidates']
        if history_reference is not None:
            # Reading an archive is explicit. Its words stay historical evidence,
            # never become new input_output, and never overwrite the active scene.
            if (history_reference.get('generation') != snapshot['generation']
                    or any(snapshot['codes'].get(code) != oid
                           for code, oid in history_reference.get('codes', {}).items())):
                raise ValueError('历史片场已被人工纠正或人物映射不一致')
            if history_reference['at'] > now_ms:
                raise ValueError('不能引用未来片场')
            payload['history_reference'] = {'scene': history_reference['scene'], 'at': history_reference['at'],
                'scope': 'historical_only'}
        from .jev_input import compact_jev_input
        from .jev_material import blocks
        material = compact_jev_input(payload)
        user = blocks([('上一版当前现场', {'scene': material['scene'], 'scene_at': material['scene_at']} if material['scene'] else None),
            ('本批新事件', material['input_output']), ('前序依据', material['recent_context']),
            ('人物依据', material['identity_evidence']), ('主认知参考', material['main_scene']),
            ('人物映射', material['people']), ('历史候选', material.get('history_candidates')),
            ('历史参考', material.get('history_reference')),
            ('材料范围', {'now': material['now'], 'time_basis': material.get('time_basis'), **material['window']})])
        schema = deepcopy(SCENE_SCHEMA)
        history_codes = [row['h'] for row in snapshot.get('history_candidates', [])]
        # The instruction allows an empty array even without history candidates.
        schema['properties']['discard_history'] = {'type': 'array', 'uniqueItems': True,
            'maxItems': len(history_codes),
            'items': {'type': 'string', **({'enum': history_codes} if history_codes else {})}}
        request = ModelRequest(purpose="voice_jev_scene", input_text=user, persona_user_text=user,
            subject_state=SubjectState(subject_id, "匠石", "整理JEV片场"),
            system_extra=SCENE_INSTRUCTION + "\nJSON Schema：" + json.dumps(schema, ensure_ascii=False))
        if on_request:
            on_request(request)
        try:
            raw = self.model.generate(request).text
        except Exception as exc:
            from .jev_errors import failure_details
            snapshot['failure'] = failure_details(exc, 'model_call')
            if (getattr(exc, 'status', None) or getattr(exc, 'code', None)) in {401, 402, 403}:
                self._unavailable_until = monotonic() + 60
            raise
        snapshot["model_raw"] = raw
        try:
            data = parse_json_object(raw)
        except Exception as exc:
            from .jev_errors import failure_details
            snapshot['failure'] = failure_details(exc, 'output_json')
            raise
        scene = data.get("scene")
        if not isinstance(scene, str) or not scene.strip() or len(scene) > 1000:
            raise ValueError("JEV片场无效或超过1000字符；待办保持未处理")
        discarded = data.get('discard_history', [])
        if (not isinstance(discarded, list) or any(not isinstance(h, str) or h not in history_codes for h in discarded)
                or len(discarded) != len(set(discarded))):
            raise ValueError('片场历史删除引用非法')
        from .jev_serial import validate
        # A copied input timestamp has no authority over event or commit time.
        # Retain it diagnostically; all other extra fields remain strict errors.
        if 'scene_at' in data:
            snapshot['ignored_output_fields'] = {'scene_at': data['scene_at']}
            data = {k: v for k, v in data.items() if k != 'scene_at'}
        validate(data, schema)
        snapshot['discard_history'] = discarded
        return scene, raw
