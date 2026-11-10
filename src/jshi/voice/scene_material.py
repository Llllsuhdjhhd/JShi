"""Model-facing dialogue first; detailed identity evidence has a separate budget."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def dialogue_row(payload):
    keys = ('ref', 'p', 'text', 'at', 'basis', 'status', 'track', 'entry_status', 'entry_interaction', 'targets',
            'scope', 'corrected_events', 'environment', 'kind', 'source')
    row = {k: deepcopy(payload[k]) for k in keys if k in payload}
    judgment = payload.get('judgment') or {}
    if judgment:
        row['identity'] = {k: judgment[k] for k in ('person', 'certainty') if k in judgment}
    return row


def identity_evidence(payload):
    """One compact comparison; never repeat synthetic short-audio candidates."""
    voice = payload.get('voice') or {}
    result = {'voice': {k: voice[k] for k in ('pick', 'strength') if k in voice}} if voice else {}
    joint = payload.get('joint_evidence') or {}
    if joint:
        result['joint'] = {k: joint[k] for k in ('state', 'voice_reliable', 'mlp_high', 'mlp_priority') if k in joint}
    candidates = []
    for item in payload.get('candidates', ()):
        from .jev_material import support_evidence
        candidates.append(support_evidence(item))
    if candidates:
        result['candidates'] = candidates
    why = (payload.get('judgment') or {}).get('why')
    if why:
        result['entry_reason'] = why
    if payload.get('short_voice_hint'):
        result['short_audio'] = '同句重复拼接，仅辅助比较，不增加独立证据'
    return result


def recent_scene_blocks(main_scene, cap=800):
    """Only recent references, never resurrect an old question as today's task."""
    blocks = re.split(r'\n(?=B\d+(?:\[|\s))', main_scene.strip()) if main_scene.strip() else []
    kept, size = [], 0
    for block in reversed(blocks):
        age = re.match(r'B\d+\[([^]]+)\]', block)
        if not age:
            continue  # Undated persona/standing material is not a recent event.
        if age:
            label = age[1]
            minutes = re.fullmatch(r'(\d+)分钟前', label)
            if not ('秒前' in label or label in {'刚才', '现在'} or minutes):
                continue
        block = re.sub(r'^B\d+(?=\[)', '', block)
        from jshi.core.scene_text import observation_text
        block = observation_text(block)
        if size + len(block) <= cap:
            kept.insert(0, block)
            size += len(block)
    return '\n'.join(kept), len(blocks) - len(kept)
