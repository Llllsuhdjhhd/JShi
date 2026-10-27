"""Deterministic evidence selection. No model calls and no database writes."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from jshi.textutil import query_terms


def representation_version(item) -> str:
    return sha256((item.content or item.text).encode('utf-8')).hexdigest()[:16]


def is_continuation(query: str) -> bool:
    return query.strip().rstrip('。？！?!') in {'继续', '接着说', '然后呢', '刚才呢', 'continue'}


def applicable(query: str, previous_query: str, content: str, threshold: float) -> bool:
    """Lexical reuse is conservative; an explicit continuation can reuse its scope."""
    left, right = query_terms(query), query_terms(previous_query)
    if not left:
        return False
    if is_continuation(query):
        return True
    overlap = len(left & right) / max(1, min(len(left), len(right)))
    return overlap >= threshold


def select_candidates(candidates, *, max_items: int, max_chars: int):
    """Preserve backend order, explicit corrections, and complete representations."""
    rows = tuple(candidates)
    superseded = {ref for item in rows for ref in getattr(item, 'supersedes', ())}
    seen, kept, skipped = set(), [], []
    size = 0
    for item in rows:
        if item.event_id in seen:
            skipped.append((item.event_id, 'duplicate'))
            continue
        seen.add(item.event_id)
        if item.event_id in superseded or getattr(item, 'validity', 'active') == 'superseded':
            skipped.append((item.event_id, 'superseded'))
            continue
        text = (item.content or item.text).strip()
        if not text:
            skipped.append((item.event_id, 'invalid_source'))
            continue
        if len(kept) >= max_items or size + len(text) > max_chars:
            skipped.append((item.event_id, 'budget'))
            continue
        kept.append(replace(item, content=text))
        size += len(text)
    return tuple(kept), tuple(skipped)
