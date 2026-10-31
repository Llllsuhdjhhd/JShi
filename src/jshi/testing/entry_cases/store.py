"""Independent, immutable media and versioned manually authored scenarios."""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
from uuid import uuid4
import wave
from datetime import datetime, timezone


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CaseStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, *parts):
        for part in parts:
            if not re.fullmatch(r'[A-Za-z0-9_.-]+', str(part)) or str(part) in {'.', '..'}:
                raise ValueError('无效的场景或素材编号')
        path = self.root.joinpath(*parts).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('路径超出场景库')
        return path

    def import_wav(self, data, *, source=None, receipts=None):
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise ValueError('当前回放需要16kHz、单声道、16位PCM WAV；请先转换，原件另行保留')
            frames = audio.getnframes()
            pcm = audio.readframes(frames)
            if not frames or len(pcm) != frames * 2:
                raise ValueError('录音为空或不完整')
        if receipts is not None:
            cursor, last = 0, -1
            for row in receipts:
                if (type(row.get('start_sample')) is not int or type(row.get('samples')) is not int
                        or row['start_sample'] != cursor or row['samples'] <= 0
                        or not isinstance(row.get('received_ms'), (int, float))
                        or not math.isfinite(row['received_ms']) or row['received_ms'] < max(0, last)):
                    raise ValueError('接收时间表必须连续、有序且时间有效')
                cursor += row['samples']; last = row['received_ms']
            if cursor != frames:
                raise ValueError('接收时间表没有覆盖完整录音')
        # Unique imports preserve different receipt histories of identical waveforms.
        ident = uuid4().hex
        folder = self.path('media', ident)
        folder.mkdir(parents=True)
        try:
            (folder / 'original.wav').write_bytes(data)
            write_json(folder / 'metadata.json', {'id': ident, 'sha256': hashlib.sha256(data).hexdigest(),
                'frames': frames, 'duration_ms': frames / 16, 'source': source,
                'receipts': receipts, 'timing_basis': 'recorded' if receipts is not None else 'reconstructed'})
        except BaseException:
            for file in folder.iterdir():
                file.unlink()
            folder.rmdir()
            raise
        return ident

    def validate(self, case):
        if case.get('schema_version') != 1 or not str(case.get('name', '')).strip():
            raise ValueError('场景需要版本1和名称')
        if not isinstance(case.get('timeline'), list):
            raise ValueError('timeline须为事件列表')
        for event in case['timeline']:
            at = event.get('at_ms')
            if not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0:
                raise ValueError('事件时间须为非负毫秒')
            if event.get('type') not in {'audio', 'jev'}:
                raise ValueError('当前支持audio及冻结JEV事件，其他事件尚未接入')
            if event['type'] == 'audio':
                if not self.path('media', event['media'], 'metadata.json').is_file():
                    raise ValueError('录音素材不存在')
            elif not isinstance(event.get('batch'), list) or not event['batch']:
                raise ValueError('JEV事件需要非空batch')
        return case

    def save(self, case):
        self.validate(case)
        ident = case.get('id') or uuid4().hex
        version = uuid4().hex
        current = self.path('scenarios', ident, 'current.json')
        previous = json.loads(current.read_text(encoding='utf-8')) if current.exists() else None
        now = datetime.now(timezone.utc).isoformat()
        created = self._saved_time(previous)[0] if previous else now
        basis = self._saved_time(previous)[1] if previous else 'recorded'
        value = {**case, 'id': ident, 'version': version,
                 'saved_at': created, 'saved_at_basis': basis, 'updated_at': now}
        write_json(self.path('scenarios', ident, version + '.json'), value)
        write_json(self.path('scenarios', ident, 'current.json'), value)
        return value

    def load(self, ident):
        return self.validate(json.loads(self.path('scenarios', ident, 'current.json').read_text(encoding='utf-8')))

    def list(self):
        values = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(self.root.glob('scenarios/*/current.json'))]
        return [{**v, 'saved_at': self._saved_time(v)[0], 'saved_at_basis': self._saved_time(v)[1]} for v in values]

    def _saved_time(self, case):
        if case.get('saved_at'):
            return case['saved_at'], case.get('saved_at_basis', 'recorded')
        # Historical cases have no wall-clock field. File time is only a
        # fallback for saving, never evidence of original microphone capture.
        files = list(self.path('scenarios', case['id']).glob('*.json'))
        return datetime.fromtimestamp(min(p.stat().st_mtime for p in files), timezone.utc).isoformat(), 'file_time'
