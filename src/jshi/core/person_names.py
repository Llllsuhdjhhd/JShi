"""Known names at model boundaries; internal object and session references stay stable."""
from collections import Counter
import json
import re


def known_names(codes, profiles):
    candidates = {}
    if profiles is None:
        return candidates
    rows = profiles.list()
    by_id = {profile.object_id: profile for profile in rows}
    for code, oid in codes.items():
        profile = by_id.get(oid)
        if profile is None or profile.status == 'rejected':
            continue
        name = profile.label.strip()
        if (not name or name.startswith(('待定声音', '未命名', '身份未确认', '声音归属', '语音现场'))
                or profile.source in {'voice_scene'}
                or name in {'unknown', 'new', 'none'} or re.fullmatch(r'[PS]\d+', name)):
            continue
        candidates[code] = name
    # A duplicated human name is not an unambiguous model identifier.
    counts = Counter(p.label.strip() for p in rows if p.status != 'rejected')
    return {code: name for code, name in candidates.items() if counts[name] == 1}


class PersonNames:
    def __init__(self, aliases=()):
        self.aliases = dict(aliases)
        self.reverse = {name: code for code, name in self.aliases.items()}

    def prose(self, text):
        # Generated context may quote actual speech about "P3". Preserve quotes.
        protected = {}
        def hold(match):
            marker = f'\x00UTTERANCE_{len(protected)}\x00'
            protected[marker] = match[2]
            return match[1] + marker
        speakers = '|'.join(re.escape(value) for value in (*self.aliases, *self.aliases.values()))
        if speakers:
            text = re.sub(rf'(?m)^((?:(?:A|B)\d+[^\n]*?\s+)?(?:{speakers})(?:（[^）]*）)?：)([^\n]*)', hold, text)
        pieces = re.split(r'(“[^”]*”|「[^」]*」|`[^`]*`)', text)
        for i in range(0, len(pieces), 2):
            for code, name in sorted(self.aliases.items(), key=lambda row: len(row[0]), reverse=True):
                pieces[i] = re.sub(rf'(?<![A-Za-z0-9_]){re.escape(code)}（{re.escape(name)}）', lambda m: name, pieces[i])
                pieces[i] = re.sub(rf'(?<![A-Za-z0-9_]){re.escape(code)}(?![A-Za-z0-9_])', lambda m: name, pieces[i])
        result = ''.join(pieces)
        for marker, value in protected.items():
            result = result.replace(marker, value)
        return result

    def value(self, value, key='', reverse=False):
        mapping = self.reverse if reverse else self.aliases
        if isinstance(value, dict):
            return {mapping.get(k, k) if key in {'people', '人物映射'} else k:
                    self.value(v, k, reverse) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v, key, reverse) for v in value]
        if isinstance(value, str):
            if key in {'p', 'person', 'pick', 'owner', 'targets', 'target_ids', 'person_code', 'reply_targets', 'speaker_pick', 'voice_pick', 'semantic_pick'}:
                return mapping.get(value, value)
            if not reverse and key in {'scene', 'main_scene', 'recent_context', 'why', 'basis', 'entry_reason', 'decision_reason', 'semantic_reason'}:
                return self.prose(value)
        return value

    def schema(self, value):
        if isinstance(value, dict):
            return {k: [self.aliases.get(x, x) if isinstance(x, str) else x for x in v]
                    if k == 'enum' else self.schema(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.schema(v) for v in value]
        return value

    def material(self, text):
        if not self.aliases:
            return text
        parts = re.split(r'(?m)(^【[^\n】]+】\n)', text)
        result = [parts[0]]
        for i in range(1, len(parts), 2):
            header, body = parts[i], parts[i + 1]
            try:
                payload = json.loads(body)
            except (ValueError, TypeError):
                # Input text follows a host-generated speaker prefix; its actual
                # utterance after the colon remains verbatim.
                if '此时的输入' in header:
                    body = re.sub(r'(?m)^(\d+\. )([^：\n]+)(：)',
                        lambda m: m[1] + self.prose(m[2]) + m[3], body)
                else:
                    body = self.prose(body)
            else:
                payload = self.value(payload)
                if '人物映射' in header and isinstance(payload, dict):
                    payload = {self.aliases.get(k, k): v for k, v in payload.items()}
                body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + ('\n\n' if body.endswith('\n\n') else '')
            result.extend((header, body))
        return ''.join(result)
