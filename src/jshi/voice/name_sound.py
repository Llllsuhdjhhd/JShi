"""Leading-vocative name-sound hints for JEV (toneless syllable overlap).

Uses a small built-in table covering names_for_jiangshi and common vocative
homophones; no pypinyin dependency.
"""
from __future__ import annotations

import re
from collections import Counter

# Toneless pinyin for characters that appear in addressee names and vocative ASR variants.
_PINYIN: dict[str, str] = {
    '匠': 'jiang', '将': 'jiang', '江': 'jiang', '姜': 'jiang', '豇': 'jiang',
    '石': 'shi', '室': 'shi', '士': 'shi', '事': 'shi', '市': 'shi', '试': 'shi',
    '诗': 'shi', '师': 'shi', '史': 'shi', '是': 'shi',
    '匚': 'fang',  # rare glyph sometimes seen in nickname lists; not a 匠 homophone
    '连': 'lian', '連': 'lian',
    '军': 'jun', '軍': 'jun',
    '小': 'xiao', '左': 'zuo',
    '阿': 'a', '啊': 'a',
}

# Strip polite / vocative suffixes from names_for_jiangshi entries before syllabifying.
_NAME_SUFFIX = re.compile(r'(?:同学|老师|先生|女士|啊|呀|呢|吧|嘛|哟)+$')

# Chinese vocative name part: ≤4 CJK, then comma / particle / ellipsis / EOS / clause break.
_VOCATIVE = re.compile(
    r'^([\u4e00-\u9fff]{1,4})'
    r'(?:[，,、…\.\s]+|\.{2,}|[啊呀呢吧嘛哟]+|(?=[？?！!])|$)'
)
_LATIN_WORD = re.compile(r'[A-Za-z]+')


def _syllables(text: str) -> list[str] | None:
    """Return toneless syllables, or None if any character lacks a table entry."""
    out: list[str] = []
    for ch in text:
        if ch.isspace():
            continue
        if 'A' <= ch <= 'Z' or 'a' <= ch <= 'z':
            # Latin tokens are kept as whole lowercased units (Ross / Phoebe).
            out.append(ch.lower())
            continue
        py = _PINYIN.get(ch)
        if py is None:
            return None
        out.append(py)
    return out


def _skip_leading_latin(text: str) -> str:
    """Ignore leading Latin/ASCII letter words and whitespace before the CJK vocative."""
    pos = 0
    n = len(text)
    while pos < n:
        while pos < n and text[pos].isspace():
            pos += 1
        match = _LATIN_WORD.match(text, pos)
        if not match:
            break
        pos = match.end()
    return text[pos:]


def leading_vocative_token(text: str) -> str:
    """Extract the leading CJK vocative (≤4 chars), allowing a Latin prefix before it."""
    raw = _skip_leading_latin((text or '').strip())
    if not raw:
        return ''
    match = _VOCATIVE.match(raw)
    return match.group(1) if match else ''


def _name_core(name: str) -> str:
    return _NAME_SUFFIX.sub('', (name or '').strip())


def _matched_count(token_syllables: list[str], name_syllables: list[str]) -> int:
    """How many name syllables are covered by the token (multiset consumption)."""
    bag = Counter(token_syllables)
    matched = 0
    for syl in name_syllables:
        if bag[syl] > 0:
            bag[syl] -= 1
            matched += 1
    return matched


def name_sound_hint(text: str, names: list[str] | tuple[str, ...] | None) -> dict | None:
    """Build ``name_sound_hint`` when the leading vocative overlaps an addressee name.

    Returns e.g. ``{token, target, matched: 'k/n', position: 'vocative'}`` or None.
    ``matched`` is always ``k/n`` with n = syllable count of the chosen target name.
    """
    token = leading_vocative_token(text)
    if not token:
        return None
    token_syl = _syllables(token)
    if not token_syl:
        return None
    best: tuple[int, int, str] | None = None  # (matched, name_len, target)
    for raw in names or ():
        core = _name_core(str(raw))
        if len(core) < 1:
            continue
        name_syl = _syllables(core)
        if not name_syl:
            continue
        k = _matched_count(token_syl, name_syl)
        if k < 1:
            continue
        n = len(name_syl)
        candidate = (k, len(core), core)
        if best is None or candidate[0] > best[0] or (
                candidate[0] == best[0] and candidate[1] > best[1]):
            best = candidate
            best_n = n
    if best is None:
        return None
    k, _, target = best
    return {
        'token': token,
        'target': target,
        'matched': f'{k}/{best_n}',
        'position': 'vocative',
    }


def weak_hint_yields_to_scene(hint: dict | None, scene: str) -> bool:
    """Rule: weak name_sound_hint (k < n) yields when the vocative token names a scene person."""
    if not hint or not scene:
        return False
    matched = str(hint.get('matched') or '')
    if '/' not in matched:
        return False
    try:
        k_s, n_s = matched.split('/', 1)
        k, n = int(k_s), int(n_s)
    except ValueError:
        return False
    if not (0 < k < n):
        return False
    token = str(hint.get('token') or '')
    if not token:
        return False
    # Presence signal: scene text mentions the vocative token as a person on site.
    return token in scene
