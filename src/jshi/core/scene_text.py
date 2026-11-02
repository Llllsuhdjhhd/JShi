"""Project known host observation headers without changing stored evidence."""
from datetime import datetime, timezone, timedelta
import re

BEIJING = timezone(timedelta(hours=8))


def observation_text(text):
    def compact(match):
        try:
            stamp = float(match[1])
            when = datetime.fromtimestamp(stamp / 1000, BEIJING).strftime('%Y-%m-%d %H:%M:%S') if stamp > 0 else ''
        except (ValueError, OverflowError, OSError):
            when = ''
        return f'[记录于{when}]' if when else '[记录时间未知]'
    return re.sub(r'\[入口观察，input_id=[^,，\]\s]+[,，]\s*at=([0-9.]+)\]', compact, text)


def scoped_voice_label(label, object_id):
    # Stable anonymous visitors already have persistent profile identifiers.
    if label.startswith('待定声音 ') and object_id:
        from hashlib import sha256
        return label + '〔声音' + sha256(object_id.encode()).hexdigest()[:10] + '〕'
    return label
