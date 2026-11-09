"""One line per model HTTP call: connection reuse, timings and token usage. Never prompts or keys."""
from __future__ import annotations

from pathlib import Path
from time import time

_path: Path | None = None


def configure(path: Path | None) -> None:
    global _path
    _path = Path(path) if path is not None else None


def usage_fields(usage) -> dict:
    """DeepSeek reports cache hits separately; OpenAI-style providers nest cached_tokens."""
    if not isinstance(usage, dict):
        return {}
    details = usage.get('prompt_tokens_details') or {}
    completion = usage.get('completion_tokens_details') or {}
    hit = usage.get('prompt_cache_hit_tokens')
    if hit is None and isinstance(details, dict):
        hit = details.get('cached_tokens')
    row = {'prompt_tokens': usage.get('prompt_tokens', usage.get('promptTokenCount')),
           'cache_hit_tokens': hit if hit is not None else usage.get('cachedContentTokenCount'),
           'cache_miss_tokens': usage.get('prompt_cache_miss_tokens'),
           'completion_tokens': usage.get('completion_tokens', usage.get('candidatesTokenCount')),
           'reasoning_tokens': completion.get('reasoning_tokens') if isinstance(completion, dict) else None}
    return {k: v for k, v in row.items() if v is not None}


def record(row: dict) -> None:
    if _path is None:
        return
    from jshi.voice.diagnostics import diagnostic_log
    diagnostic_log(_path).write({'at_ms': round(time() * 1000), **row})
