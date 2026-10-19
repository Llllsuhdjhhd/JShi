"""Import a small AISHELL-1 background pool without adding fictional local people."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import wave

from .personal_store import PersonalStore
from .personal_model import partition

SOURCE = 'https://www.openslr.org/33/'
MIRROR = 'https://huggingface.co/datasets/carlot/AIShell'


def decode_clip(data, clip):
    with wave.open(io.BytesIO(data), 'rb') as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (16000, 1, 2, 'NONE'):
            raise ValueError('公共录音须为16kHz单声道16bit PCM WAV')
        pcm = wav.readframes(wav.getnframes())
    if len(pcm) < 4*32000:
        return None
    # One crop per original file, with a stable length; never repeat or pad.
    length = (.6, 1.5, 4.)[int(hashlib.sha256(clip.encode()).hexdigest(), 16) % 3]
    size = int(length*32000)
    start = (len(pcm)-size)//4*2
    crop = pcm[start:start+size]
    # Avoid importing a center crop containing only silence.
    import numpy as np
    values = np.frombuffer(crop, dtype='<i2').astype('float32') / 32768
    return crop if float(np.sqrt(np.mean(values*values))) >= .003 else None


def import_parquet(paths, store, *, per_speaker=36):
    import pyarrow.parquet as pq
    counts = Counter()
    seen = set()
    # Quotas apply to the saved pool, including earlier incremental imports.
    for row in store.background_samples():
        if row['dataset'] == 'aishell-1':
            seen.add(row['clip'])
            counts[row['speaker']] += 1
            bucket = int(hashlib.sha256(row['clip'].encode()).hexdigest(), 16) % 3
            counts[(row['speaker'], bucket)] += 1
    added = 0
    for path in map(Path, paths):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=32, columns=['audio']):
            for record in batch.to_pylist():
                audio = record['audio']
                name = Path(audio.get('path') or '').name
                if name in seen:
                    continue
                match = re.fullmatch(r'BAC009(S\d+)W\d+\.wav', name)
                if not match:
                    raise ValueError('录音缺少可验证的AISHELL原始说话人编号')
                speaker = match[1]
                if counts[speaker] >= per_speaker:
                    continue
                bucket = int(hashlib.sha256(name.encode()).hexdigest(), 16) % 3
                quota = per_speaker//3 + int(bucket < per_speaker % 3)
                if counts[(speaker, bucket)] >= quota:
                    continue
                pcm = decode_clip(audio['bytes'], name)
                if pcm is None:
                    continue
                store.save_background(pcm, dataset='aishell-1', speaker=speaker, clip=name,
                                      source_url=SOURCE, license='Apache-2.0')
                seen.add(name)
                counts[speaker] += 1
                counts[(speaker, bucket)] += 1
                added += 1
    public = store.background_samples()
    return {'processed': added, 'clips': len(public),
            'speakers': len({(r['dataset'], r['speaker']) for r in public}),
            'groups': dict(Counter(partition(r['session_id']) for r in public))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--parquet', nargs='+', required=True)
    parser.add_argument('--per-speaker', type=int, default=36)
    args = parser.parse_args()
    if not 1 <= args.per_speaker <= 100:
        parser.error('每个公开说话人限1–100条原始录音')
    store = PersonalStore(Path(args.store))
    report = import_parquet(args.parquet, store, per_speaker=args.per_speaker)
    attribution_path = store.root/'background_attribution.json'
    previous = json.loads(attribution_path.read_text(encoding='utf-8')) if attribution_path.exists() else {}
    attribution = {'dataset': 'AISHELL-1', 'source': SOURCE, 'mirror': MIRROR,
        'license': 'Apache-2.0', 'citation': 'Hui Bu et al., AIShell-1, Oriental COCOSDA 2017',
        'purpose': '公开说话人背景反例；不代表任何本地注册人物',
        'files': list(previous.get('files', []))}
    for path in map(Path, args.parquet):
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024*1024), b''):
                digest.update(block)
        item = {'name': path.name, 'sha256': digest.hexdigest()}
        selection_path = path.with_suffix('.json')
        if selection_path.exists():
            selection = json.loads(selection_path.read_text(encoding='utf-8'))
            if selection.get('subset_sha256') != item['sha256']:
                raise ValueError('公开语料子集与来源记录的校验值不一致')
            item['selection'] = selection
        if item not in attribution['files']:
            attribution['files'].append(item)
    attribution_path.write_text(json.dumps(attribution, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
