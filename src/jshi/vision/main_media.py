"""Select original images once per cognition context; commit only after success."""
import base64
from hashlib import sha256
from threading import RLock


class MainImageGate:
    def __init__(self):
        self.seen = {}
        self.lock = RLock()

    def prepare(self, parts, *, subject, context, allowed, store, force=False):
        selected, tokens, references = [], [], []
        local = set()
        with self.lock:
            seen = self.seen.get((subject, context), set())
            for part in parts:
                if part.get('kind') != 'image':
                    selected.append(part)
                    continue
                if not allowed:
                    continue
                reference = part.get('reference', '')
                try:
                    if reference:
                        data = store.image(reference, subject)
                    else:
                        url = part.get('image_url', '')
                        if not url.startswith('data:image/') or ';base64,' not in url:
                            continue
                        data = base64.b64decode(url.split(';base64,', 1)[1], validate=True)
                except (KeyError, ValueError):
                    continue
                token = sha256(data).hexdigest()
                if token in local or (token in seen and not force):
                    continue
                local.add(token)
                tokens.append(token)
                if reference:
                    references.append(reference)
                number = len(tokens)
                selected.append({'kind':'image', 'reference':f'M{number}', 'label':part.get('label') or f'图片{number}',
                                 'image_url':'data:image/jpeg;base64,' + base64.b64encode(data).decode()})
        return tuple(selected), tuple(tokens), tuple(references)

    def commit(self, subject, context, tokens):
        with self.lock:
            self.seen.setdefault((subject, context), set()).update(tokens)

    def reset(self, subject, context):
        with self.lock:
            self.seen.pop((subject, context), None)
