"""Import public TV/news recordings as negatives, never as one local person."""
from __future__ import annotations

import io
import wave
from pathlib import Path

SOURCE = 'https://github.com/joonson/voxconverse'
MIRROR = 'https://huggingface.co/datasets/diarizers-community/voxconverse'


def television_crops(data, *, per_recording=12):
    import numpy as np
    if not 1 <= per_recording <= 36:
        raise ValueError('每条节目保留1–36个片段')
    with wave.open(io.BytesIO(data), 'rb') as wav:
        if (wav.getframerate(), wav.getsampwidth(), wav.getcomptype()) != (16000, 2, 'NONE'):
            raise ValueError('电视反例须为16kHz、16bit PCM WAV')
        values = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
        channels = wav.getnchannels()
        if channels > 1:
            values = values.reshape(-1, channels).astype('float32').mean(axis=1).astype('<i2')
    seconds = len(values)/16000
    if seconds < .6:
        return []
    result, previous_end = [], 0
    for i in range(per_recording):
        duration = (.6, 1.5, 4.)[i % 3]
        length = int(min(duration, seconds)*16000)
        start = int(i*max(0,len(values)-length)/max(1,per_recording-1))
        if start < previous_end:
            continue
        crop = values[start:start+length]
        if float(np.sqrt(np.mean((crop.astype('float32')/32768)**2))) < .003:
            continue
        result.append((start//16, crop.astype('<i2').tobytes()))
        previous_end = start+length
    return result


def import_tv_records(records, store, *, shard, per_recording=12):
    before = {r['id'] for r in store.background_samples()}
    saved, originals = set(), set()
    for index, record in enumerate(records):
        audio = record.get('audio') or {}
        data = audio.get('bytes')
        if not isinstance(data, bytes):
            raise ValueError('节目录音缺少嵌入的原始音频，不能伪造反例')
        clip = str(audio.get('path') or record.get('id') or f'{shard}:row-{index}')
        for offset, pcm in television_crops(data, per_recording=per_recording):
            # A corpus may repeat speakers across programmes without global IDs.
            # Keep the complete collection on one side of train/holdout splits.
            sid = store.save_background(pcm, dataset='voxconverse', speaker='broadcast-training-collection',
                clip=f'{clip}:{offset}ms:{len(pcm)}', source_url=SOURCE, license='CC-BY-4.0')
            saved.add(sid); originals.add(clip)
    return {'added':len(saved-before), 'clips':len(saved), 'original_recordings':len(originals),
        'dataset':'voxconverse', 'source':SOURCE, 'mirror':MIRROR,
        'license':'CC-BY-4.0', 'grouping':'collection; not one person',
        'purpose':'电视新闻/辩论等公开媒体声音反例；不创建人物档案'}


def import_tv_parquet(paths, store, *, per_recording=12):
    import pyarrow.parquet as pq
    reports = []
    for filename in map(Path, paths):
        for batch in pq.ParquetFile(filename).iter_batches(batch_size=1):
            reports.append(import_tv_records(batch.to_pylist(), store, shard=filename.name,
                per_recording=per_recording))
    return {'added':sum(r['added'] for r in reports),
        'clips':sum(r['clips'] for r in reports),
        'original_recordings':sum(r['original_recordings'] for r in reports),
        'reports':reports}
