"""Select original images once per cognition context; commit only after success."""
import base64
from hashlib import sha256
from threading import RLock
from time import monotonic


class MainImageGate:
    def __init__(self):
        self.seen = {}
        self.lock = RLock()
        self.last_recalled = {}

    def prepare(self, parts, *, subject, context, allowed, store, force=False, recalled_allowed=False, delivered_overviews=None, delivered_recent=None):
        selected, tokens, references = [], [], []
        local = {}
        used_bytes = 0
        omitted = []
        with_memory = any(p.get("role") == "memory_overview" for p in parts)
        with self.lock:
            seen = self.seen.get((subject, context), set())
            for part in parts:
                if part.get('kind') != 'image':
                    selected.append(part)
                    continue
                role = part.get('role', 'current')
                historical = role in {'memory_overview', 'memory_crop'}
                if not (recalled_allowed if historical else allowed):
                    continue
                reference = part.get('reference', '')
                try:
                    if historical and reference:
                        data = store.memory_view(subject, reference, part.get('target',''), part.get('region'))
                    elif reference:
                        data = store.image(reference, subject)
                    else:
                        url = part.get('image_url', '')
                        if not url.startswith('data:image/') or ';base64,' not in url:
                            continue
                        data = base64.b64decode(url.split(';base64,', 1)[1], validate=True)
                except (KeyError, ValueError):
                    if historical:
                        omitted.append((part.get('label') or '历史画面') + '：像素不可用')
                    continue
                token = sha256(data).hexdigest()
                label = part.get('label') or '当前画面'
                if historical:
                    label = f"{label} · 回忆画面 · {part['time_label']}"
                recipients = delivered_overviews if role=='memory_overview' else delivered_recent if role=='recent' else None
                if token in local:
                    if recipients is not None:
                        recipients.append(part)
                    duplicate = local[token]
                    if label not in duplicate['label']:
                        duplicate['label'] += ' / ' + label
                    continue
                if role=='current' and token in seen and not (force or with_memory):
                    continue
                if used_bytes + len(data) > store.config.main_image_bytes:
                    omitted.append(label + '：图片预算已满')
                    continue
                used_bytes += len(data)
                if recipients is not None:
                    recipients.append(part)
                if role=='current':
                    tokens.append(token)
                if reference and role=='current':
                    references.append(reference)
                number = len(local)+1
                selected.append({'kind':'image', 'reference':f'M{number}', 'label':label, 'role':role,
                                 'image_url':'data:image/jpeg;base64,' + base64.b64encode(data).decode()})
                local[token] = selected[-1]
        if omitted:
            selected.append({'kind':'media_status','text':'本次未附图：'+'；'.join(omitted)})
        return tuple(selected), tuple(tokens), tuple(references)

    def remember_recall(self, key, overview):
        with self.lock:
            now = monotonic()
            self.last_recalled = {k:v for k,v in self.last_recalled.items() if now-v[0]<=60}
            if overview:
                self.last_recalled[key] = (now, dict(overview))
            else:
                self.last_recalled.pop(key, None)

    def recall_followup(self, key):
        with self.lock:
            saved = self.last_recalled.get(key)
            return dict(saved[1]) if saved and monotonic()-saved[0]<=60 else None

    def commit(self, subject, context, tokens):
        with self.lock:
            self.seen.setdefault((subject, context), set()).update(tokens)

    def reset(self, subject, context):
        with self.lock:
            self.seen.pop((subject, context), None)
            for key in list(self.last_recalled):
                if key[:2] == (subject, context):
                    del self.last_recalled[key]
