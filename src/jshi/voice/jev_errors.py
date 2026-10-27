"""Failure categories describe the failed operation, never a routing decision."""
from urllib.error import URLError
import re


class JEVStaleError(RuntimeError):
    pass


class JEVCooldownError(RuntimeError):
    pass


def failure_details(exc, phase='internal'):
    status = getattr(exc, 'status', None) or getattr(exc, 'code', None)
    if status in (401, 403):
        kind = 'authentication'
    elif status == 402:
        kind = 'billing'
    elif status == 429:
        kind = 'rate_limit'
    elif (status == 408 or isinstance(exc, TimeoutError)
          or isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)):
        kind = 'timeout'
    elif isinstance(exc, JEVStaleError):
        kind = 'stale'
    elif isinstance(exc, JEVCooldownError):
        kind = 'cooldown'
    elif status:
        kind = 'provider_http'
    elif isinstance(exc, (URLError, ConnectionError, OSError)):
        kind = 'transport'
    else:
        kind = phase
    message = str(exc)
    message = re.sub(r'(?i)(bearer\s+|api[_-]?key[=: ]+)[^\s,;]+', r'\1[redacted]', message)
    message = re.sub(r'sk-[A-Za-z0-9_-]+', '[redacted]', message)
    return {'error_kind': kind, 'error_type': type(exc).__name__,
            'error': f'{type(exc).__name__}: {message[:600]}',
            'http_status': status if isinstance(status, int) else None}
