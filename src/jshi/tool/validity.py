"""Explicit task deadlines, separate from execution timeout and cache age."""
from datetime import datetime, timezone


def deadline(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ValueError('任务有效期格式不完整')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('任务有效期格式不完整') from None
    if result.tzinfo is None:
        raise ValueError('任务有效期缺少时区')
    return result.astimezone(timezone.utc)


def is_expired(work, now=None):
    if not work:
        return False
    if work.get('expired'):
        return True
    try:
        limit = deadline(work.get('expires_at'))
    except ValueError:
        return True  # Invalid persisted terms cannot authorize delayed execution.
    return limit is not None and (now or datetime.now(timezone.utc)) >= limit


def pending_information(value):
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(item.strip()[:200] for item in value
                              if isinstance(item, str) and item.strip()))[:5]
