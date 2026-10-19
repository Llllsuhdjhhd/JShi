"""Small MLP format and batched CPU inference; PyTorch is needed only by training."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

import numpy as np


WIDTHS = (256, 256, 128)


def duration_bucket(seconds):
    return 'under_1s' if seconds < 1 else '1_to_3s' if seconds < 3 else '3s_plus'


def partition(session_id):
    """Split original sessions BEFORE expanding their segments; stable across retrains."""
    bucket = int(hashlib.sha256(session_id.encode()).hexdigest()[:8], 16) % 5
    return 'calibration' if bucket == 0 else 'validation' if bucket == 1 else 'train'


def split_pcm(pcm):
    """Cover all original samples once, with at most three pieces, near quiet boundaries."""
    samples = np.frombuffer(pcm, dtype='<i2')
    count = min(3, max(1, math.ceil(len(samples) / (10 * 16000))))
    cuts = [0]
    for i in range(1, count):
        center = len(samples) * i // count
        choices = range(max(cuts[-1] + 3200, center - 8000), min(len(samples)-3200, center+8000), 160)
        cut = min(choices, key=lambda j: float(np.mean(samples[j:j+160].astype('float32')**2)), default=center)
        cuts.append(cut)
    cuts.append(len(samples))
    return [pcm[a*2:b*2] for a, b in zip(cuts, cuts[1:])]


def normalize(vector):
    value = np.asarray(vector, dtype='float32')
    if value.ndim != 1 or not 1 <= len(value) <= 4096 or not np.isfinite(value).all():
        raise ValueError('无效声音特征')
    norm = float(np.linalg.norm(value))
    if not math.isfinite(norm) or norm <= 1e-8:
        raise ValueError('声音特征为空')
    return value / norm


def validate_weights(weights, dimension):
    previous = dimension
    for i, width in enumerate((*WIDTHS, 1), 1):
        if weights[f'w{i}'].shape != (previous, width) or weights[f'b{i}'].shape != (width,):
            raise ValueError('个人模型参数形状不匹配')
        if not np.isfinite(weights[f'w{i}']).all() or not np.isfinite(weights[f'b{i}']).all():
            raise ValueError('个人模型包含非有限参数')
        previous = width


def load_weights(path, dimension):
    with np.load(path, allow_pickle=False) as archive:
        result = {f'{kind}{i}': archive[f'{kind}{i}'].astype('float32') for i in range(1, 5) for kind in ('w', 'b')}
    validate_weights(result, dimension)
    for value in result.values():
        value.flags.writeable = False
    return result


def pack_weights(models):
    packed = {key: np.stack([model[key] for model in models]) for key in models[0]}
    for value in packed.values():
        value.flags.writeable = False
    return packed


def batch_scores(vectors, models, *, packed=None):
    """[segments,D] × [independent models,D,H]; no cross-person weights or softmax."""
    x = np.asarray(vectors, dtype='float32')
    if not models:
        return np.empty((0, len(x)), dtype='float32')
    packed = pack_weights(models) if packed is None else packed
    x = np.einsum('sd,mdh->msh', x, packed['w1']) + packed['b1'][:, None]
    for i in range(2, 5):
        x = np.maximum(x, 0)
        x = np.einsum('msh,mhk->msk', x, packed[f'w{i}']) + packed[f'b{i}'][:, None]
    return 1 / (1 + np.exp(-np.clip(x[..., 0], -60, 60)))


def clip_scores(vectors, durations, models, *, packed=None):
    scores = batch_scores(vectors, models, packed=packed)
    return np.average(scores, axis=1, weights=np.asarray(durations, dtype='float32'))


def atomic_json(path: Path, value):
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
