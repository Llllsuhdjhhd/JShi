"""Compact only the model-facing JEV input; source records remain unchanged."""
from __future__ import annotations

import math
import os
import re
from copy import deepcopy


_RECORD_KEYS = {
    'id', 'input_id', 'object_id', 'subject', 'subject_id', 'session_id',
    'reply_id', 'voiceprint_id', 'visual_snapshot_id', 'image_url',
    'clock_offset', 'source_time',
}
_MEDIA_KEYS = {'reference', 'frame', 'description_frame'}
_WALL_MS = {'at', 'at_ms', 'scene_at', 'received_ms', 'captured_start_ms', 'captured_end_ms'}
_WALL_SECONDS = {
    'captured', 'completed', 'described_at', 'captured_at', 'received_at', 'source_time',
    'observed_at', 'completed_at', 'description_at',
    'cached_at', 'updated_at',
}
_DURATION_MS = {'start_ms', 'end_ms', 'gap_ms'}


def compact_jev_input(payload):
    """Use request-local references and relative clocks without mutating payload."""
    now = payload.get('now')
    has_clock = type(now) in (int, float) and math.isfinite(now) and now > 0
    anchor = round(now) if has_clock else None
    media, tracks, events = {}, {}, {}

    def alias(value, names, prefix):
        if not value:
            return value
        text = str(value)
        return names.setdefault(text, f'{prefix}{len(names) + 1}')

    def clean(value, key=''):
        if isinstance(value, dict):
            if key == 'environment':
                # The observation already carries synchronized capture/description
                # clocks. Its nested producer envelope and storage fields add no facts.
                value = {k: v for k, v in value.items()
                         if k not in {'envelope', 'model', 'reason'}}
            return {k: clean(v, k) for k, v in value.items() if k not in _RECORD_KEYS}
        if isinstance(value, (list, tuple)):
            return [clean(v, key) for v in value]
        if key in _MEDIA_KEYS:
            return alias(value, media, 'M')
        if key == 'ref':
            return value if isinstance(value, str) and re.fullmatch(r'[EQ][0-9]+', value) else alias(value, events, 'R')
        if key in {'track', 'track_id'}:
            return alias(value, tracks, 'T')
        if type(value) in (int, float) and math.isfinite(value):
            if key in _WALL_MS | _WALL_SECONDS:
                if value <= 0:
                    return None  # zero denotes an unavailable wall-clock timestamp
                milliseconds = value * 1000 if key in _WALL_SECONDS else value
                return round(milliseconds - anchor) if has_clock else round(value)
            if key in _DURATION_MS:
                return round(value)
        return value

    # Remove stale visual descriptions before the model sees them. Original
    # snapshots and images remain in the host; current image availability is separate.
    payload = deepcopy(payload)
    def expire_environment(value):
        if isinstance(value, (list, tuple)):
            for item in value:
                expire_environment(item)
            return
        if not isinstance(value, dict):
            return
        for k, v in list(value.items()):
            if k == 'environment' and isinstance(v, dict) and has_clock:
                captured = v.get('described_at', v.get('description_at', v.get('captured')))
                valid = type(captured) in (int, float) and math.isfinite(captured) and captured > 0
                # Configurable observation lifetime; never substitute a new frame's
                # time for the time of an older description.
                lifetime = float(os.getenv('JSHI_JEV_VISUAL_MAX_AGE_SECONDS', '60'))
                if not math.isfinite(lifetime) or lifetime <= 0:
                    raise ValueError('JSHI_JEV_VISUAL_MAX_AGE_SECONDS 必须为正数')
                if not valid or not 0 <= now - captured * 1000 <= lifetime * 1000:
                    value[k] = {'status': 'description_stale' if valid else 'description_time_unknown',
                                'described_at': captured if valid else None,
                                'image_available': bool(v.get('image_available')),
                                'note': '当前环境未获有效描述；不得据旧描述认定当前在场者或发言者'}
            else:
                expire_environment(v)
    expire_environment(payload)
    result = clean(payload)
    envelope = result.get('input', {}).get('envelope')
    if envelope:
        # Current text is already represented by lines. Keep media associations,
        # selected-speaker evidence and timing, but do not repeat the same text.
        envelope['parts'] = [p for p in envelope.get('parts', []) if p.get('kind') != 'text']
    result['now'] = anchor if has_clock else now
    if has_clock:
        target = result['input'] if isinstance(result.get('input'), dict) else result
        target['time_basis'] = (
            '事件与环境时间均为相对now的整数毫秒：负数在此前，正数在此后；'
            'null为时间未提供。start_ms/end_ms为音频内偏移，gap_ms为间隔。'
        )
    return result
