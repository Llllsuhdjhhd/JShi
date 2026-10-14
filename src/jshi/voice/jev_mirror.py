"""Deterministic JEV mirror for person-id stubs / harnesses.

Consumes the same thin-batch fields product VoiceJEV packs (candidates,
voice_ranking, recent_confirmed, continuity.same_cluster / prior_p /
prior_judgment). Keeps same-cluster continuity without inventing a new
recognized person and without lowering Speakers thresholds.
"""
from __future__ import annotations

import json
import re

from jshi.models import ModelResponse


def _allowed_people(line: dict) -> set[str]:
    allowed = {c['p'] for c in line.get('candidates') or []
               if isinstance(c, dict) and c.get('p') and c.get('source') != 'temporary_voice'}
    if line.get('identity') != 'unresolved':
        code = str(line.get('p') or '')
        if re.fullmatch(r'P\d+', code):
            allowed.add(code)
    return allowed


def mirror_identity(line: dict) -> tuple[str, str, str]:
    """Return (person, certainty, why) for one thin input line."""
    if line.get('source') == 'visual_environment':
        return 'unknown', 'unknown', '视觉观察，无声音身份'
    who = str(line.get('p') or '')
    if line.get('source') == 'web_text' and re.fullmatch(r'P\d+', who):
        return who, 'confirmed', '文字输入已绑定所选人物'
    allowed = _allowed_people(line)
    voice = line.get('voice') if isinstance(line.get('voice'), dict) else {}
    pick = voice.get('pick') or ''
    if voice.get('strength') == 'clear' and pick in allowed:
        return pick, 'confirmed', '声纹已匹配'
    cont = line.get('continuity') if isinstance(line.get('continuity'), dict) else {}
    recent = line.get('recent_confirmed') if isinstance(line.get('recent_confirmed'), dict) else {}
    if cont.get('same_cluster'):
        judgment = cont.get('prior_judgment') or ''
        if judgment in allowed:
            return judgment, 'tentative', '同轨迹连续，沿用先前入口判断'
        if recent.get('p') in allowed and (recent.get('same_track') or True):
            return recent['p'], 'tentative', '同轨迹连续，接续近期确认人物'
        prior = cont.get('prior_p') or ''
        if prior in allowed:
            return prior, 'tentative', '同轨迹声音连续，保留同一人物'
    if recent.get('p') in allowed and recent.get('same_track'):
        return recent['p'], 'tentative', '同轨迹接续近期确认人物'
    # Ranking alone never confirms and does not invent identity without continuity.
    return 'unknown', 'unknown', '证据不足，保持未知'


class MirrorJEV:
    """Drop-in model for VoiceJEV(MirrorJEV()) used by person-id stub harnesses."""

    name = 'mirror-jev'

    def generate(self, request):
        payload = json.loads(request.input_text or '{}')
        lines = ((payload.get('input') or {}).get('lines')) or []
        items = []
        for line in lines:
            if not isinstance(line, dict) or 'i' not in line:
                continue
            person, certainty, why = mirror_identity(line)
            items.append({'i': line['i'], 'person': person, 'certainty': certainty, 'why': why[:80]})
        body = {
            'items': items,
            'level': 3,
            'recall_memory': False,
            'main_image': 'omit',
            'route': 'main',
            'reply': '',
        }
        return ModelResponse(text=json.dumps(body, ensure_ascii=False), model=self.name)
