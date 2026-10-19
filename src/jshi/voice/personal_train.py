"""Isolated CPU trainer. Never imports or mutates the running conversation."""
from __future__ import annotations

import argparse
import copy
import json
import hashlib
from pathlib import Path

import numpy as np

from .personal_model import WIDTHS, atomic_json, batch_scores, load_weights, partition


def training_record_weights(records):
    """Balance classes; device half of negatives, local labels 75% of devices.

    Raw files remain whole; this does not inflate sample counts or holdouts.
    """
    positive = sum(r['label'] == 1 for r in records)
    devices = sum(not r['label'] and r.get('device_negative', False) for r in records)
    local_devices = sum(not r['label'] and r.get('device_negative', False)
                        and r.get('local_device_negative', False) for r in records)
    public_devices = devices-local_devices
    other = sum(not r['label'] and not r.get('device_negative', False) for r in records)
    weights = []
    for row in records:
        if row['label']:
            weight = 1 / positive
        elif row.get('device_negative', False):
            device_share = .5 if other else 1.
            if row.get('local_device_negative', False):
                weight = device_share * (.75 if public_devices else 1.) / local_devices
            else:
                weight = device_share * (.25 if local_devices else 1.) / public_devices
        else:
            weight = (.5 if devices else 1.) / other
        weights.append(weight * (1. if row['source'] in {'manual', 'public'} else .35))
    return weights


def split_plan(records):
    names = ('train', 'calibration', 'validation')
    splits = {name: [r for r in records if partition(r['session_id']) == name
                   and (name == 'train' or r['source'] in {'manual', 'public'})]
            for name in names}
    def complete(parts):
        return all({r['label'] for r in parts[name]} == {0, 1} for name in names)
    if complete(splits):
        return splits, 'session'
    # Rebalance whole sessions before falling back to individual recordings.
    positive_sessions = sorted({r['session_id'] for r in records
        if r['label'] and r['source'] == 'manual'})
    if len(positive_sessions) >= 3:
        assignment = {sid: names[min(i, 2)] if i < 3 else 'train'
                      for i, sid in enumerate(positive_sessions)}
        negative_sessions = sorted({r['session_id'] for r in records if not r['label']
            and r['source'] in {'manual', 'public'}} - assignment.keys())
        for i, sid in enumerate(negative_sessions):
            assignment[sid] = names[i % 5] if i % 5 < 3 else 'train'
        parts = {name: [r for r in records if assignment.get(r['session_id'], 'train') == name
                       and (name == 'train' or r['source'] in {'manual', 'public'})] for name in names}
        if complete(parts):
            return parts, 'session'
    # Original recording IDs stay intact: never split one recording's segments.
    parts = {name: [] for name in names}
    for row in records:
        if row['source'] == 'public':
            parts[partition(row['session_id'])].append(row)
    for label in (0, 1):
        trusted = sorted([r for r in records if r['label'] == label and r['source'] == 'manual'],
            key=lambda r: hashlib.sha256(r['id'].encode()).hexdigest())
        held = max(1, len(trusted)//5) if len(trusted) >= 3 else 0
        parts['calibration'].extend(trusted[:held])
        parts['validation'].extend(trusted[held:held*2])
        parts['train'].extend(trusted[held*2:])
        parts['train'].extend(r for r in records if r['label'] == label and r['source'] not in {'manual', 'public'})
    if not any(r['label'] == 0 for r in parts['train']):
        public = [r for r in records if r['source'] == 'public']
        if public:
            sid = public[0]['session_id']
            for name in ('calibration', 'validation'):
                moved = [r for r in parts[name] if r['session_id'] == sid]
                parts[name] = [r for r in parts[name] if r['session_id'] != sid]
                parts['train'].extend(moved)
    return parts, 'recording' if complete(parts) else 'training_only'


def groups(records):
    return split_plan(records)[0]


def readiness(records, config):
    splits = groups(records)
    counts = {key: {'positive': sum(r['label'] == 1 for r in rows),
                    'negative': sum(r['label'] == 0 for r in rows)} for key, rows in splits.items()}
    return counts['train']['positive'] > 0 and counts['train']['negative'] > 0, counts


def metrics(scores, labels, threshold):
    labels = np.asarray(labels)
    accepted = np.asarray(scores) >= threshold
    positive, negative = labels == 1, labels == 0
    return {'recall': float(accepted[positive].mean()) if positive.any() else None,
            'false_accept': float(accepted[negative].mean()) if negative.any() else None,
            'positive': int(positive.sum()), 'negative': int(negative.sum())}


def score_records(records, weights):
    if not records:
        return np.asarray([])
    vectors = [v for row in records for v in row['vectors']]
    all_scores = batch_scores(vectors, [weights])[0]
    result, start = [], 0
    for row in records:
        end = start + len(row['vectors'])
        result.append(np.average(all_scores[start:end], weights=row['durations']))
        start = end
    return np.asarray(result)


def train(snapshot_path, output):
    snapshot = json.loads(Path(snapshot_path).read_text(encoding='utf-8'))
    records, config = snapshot['records'], snapshot['config']
    ready, counts = readiness(records, config)
    if not ready:
        return {'status': 'collecting', 'detail': '尚未开始训练：至少需要一条本人正例和一条反例', 'counts': counts}
    import torch
    from torch import nn

    torch.set_num_threads(1)
    torch.manual_seed(412)
    np.random.seed(412)
    dimension = snapshot['dimension']
    model = nn.Sequential(nn.Linear(dimension, WIDTHS[0]), nn.ReLU(), nn.Dropout(.15),
                          nn.Linear(WIDTHS[0], WIDTHS[1]), nn.ReLU(), nn.Dropout(.15),
                          nn.Linear(WIDTHS[1], WIDTHS[2]), nn.ReLU(), nn.Linear(WIDTHS[2], 1))
    linear = [layer for layer in model if isinstance(layer, nn.Linear)]
    previous = snapshot.get('previous')
    old_weights = None
    if previous:
        old_weights = load_weights(previous['path'], dimension)
        with torch.no_grad():
            for i, layer in enumerate(linear, 1):
                layer.weight.copy_(torch.from_numpy(old_weights[f'w{i}'].copy().T))
                layer.bias.copy_(torch.from_numpy(old_weights[f'b{i}'].copy()))

    splits, validation_mode = split_plan(records)
    x, y, sample_weights = [], [], []
    record_weights = training_record_weights(splits['train'])
    for row, record_weight in zip(splits['train'], record_weights):
        for vector, duration in zip(row['vectors'], row['durations']):
            x.append(vector)
            y.append(row['label'])
            sample_weights.append(record_weight * duration / sum(row['durations']))
    x, y = torch.tensor(np.array(x), dtype=torch.float32), torch.tensor(y, dtype=torch.float32)
    weights = torch.tensor(sample_weights, dtype=torch.float32)
    weights /= weights.mean()
    optimizer = torch.optim.AdamW(model.parameters(), lr=.001, weight_decay=.001)
    loss_fn = nn.BCEWithLogitsLoss(reduction='none')

    def export():
        return {f'{kind}{i}': (layer.weight.detach().numpy().T.copy() if kind == 'w'
                               else layer.bias.detach().numpy().copy())
                for i, layer in enumerate(linear, 1) for kind in ('w', 'b')}

    best, best_loss, patience = copy.deepcopy(model.state_dict()), float('inf'), 0
    calibration = splits['calibration']
    monitor = calibration if {r['label'] for r in calibration} == {0, 1} else splits['train']
    cal_labels = np.array([r['label'] for r in monitor])
    for _ in range(120):
        model.train()
        for batch in torch.randperm(len(x)).split(64):
            optimizer.zero_grad()
            loss = (loss_fn(model(x[batch]).squeeze(1), y[batch]) * weights[batch]).mean()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 5.)
            optimizer.step()
        model.eval()
        scores = np.clip(score_records(monitor, export()), 1e-7, 1-1e-7)
        # Calibration only chooses epoch/threshold. The validation sessions stay untouched.
        cal_loss = float(-(cal_labels*np.log(scores)+(1-cal_labels)*np.log(1-scores)).mean())
        if cal_loss < best_loss - .0001:
            best, best_loss, patience = copy.deepcopy(model.state_dict()), cal_loss, 0
        else:
            patience += 1
            if patience >= 15:
                break
    model.load_state_dict(best)
    model.eval()
    result_weights = export()
    cal_scores = score_records(calibration, result_weights)
    cal_labels = np.array([r['label'] for r in calibration])
    threshold = None
    for value in sorted(set([config['min_threshold'], *map(float, cal_scores)])):
        if value < config['min_threshold']:
            continue
        report = metrics(cal_scores, cal_labels, value)
        if (report['false_accept'] is not None and report['recall'] is not None
                and report['false_accept'] <= config['max_false_accept'] and report['recall'] >= config['min_recall']):
            threshold = value
            break
    calibrated = threshold is not None
    threshold = config['min_threshold'] if threshold is None else threshold
    validation = splits['validation']
    labels = [r['label'] for r in validation]
    scores = score_records(validation, result_weights)
    report = metrics(scores, labels, threshold)
    sufficient = all(counts[name][kind] >= config[('min_train_' if name == 'train' else 'min_eval_')+kind]
        for name in splits for kind in ('positive', 'negative'))
    verified = (validation_mode == 'session' and sufficient and calibrated
        and report['false_accept'] is not None and report['recall'] is not None
        and report['false_accept'] <= config['max_false_accept'] and report['recall'] >= config['min_recall'])
    if old_weights is not None and previous.get('verified', True):
        if not verified:
            return {'status': 'rejected', 'detail': '本次已训练，但验证不充分，保留已验证的旧模型', 'counts': counts}
        old_report = metrics(score_records(validation, old_weights), labels, previous['threshold'])
        if report['false_accept'] > old_report['false_accept'] or report['recall'] < old_report['recall']:
            return {'status': 'rejected', 'detail': '独立验证较当前版本退步，保留当前版本',
                    'validation': report, 'previous_validation': old_report, 'counts': counts}
    with open(output, 'wb') as handle:
        np.savez(handle, **result_weights)
    duration_reports = {}
    for name, low, high in [('under_1s', 0, 1), ('1_to_3s', 1, 3), ('3s_plus', 3, float('inf'))]:
        indexes = [i for i, r in enumerate(validation) if low <= sum(r['durations']) < high]
        if indexes and {labels[i] for i in indexes} == {0, 1}:
            part = metrics(scores[indexes], [labels[i] for i in indexes], threshold)
            part['enabled'] = (part['positive'] >= config['min_eval_positive']
                               and part['negative'] >= config['min_eval_negative']
                               and part['false_accept'] <= config['max_false_accept']
                               and part['recall'] >= config['min_recall'])
            duration_reports[name] = part
    for name in ('under_1s', '1_to_3s', '3s_plus'):
        part = duration_reports.setdefault(name, {'positive': 0, 'negative': 0})
        part['identity_enabled'] = verified and part.get('enabled', False)
        part['enabled'] = part['identity_enabled']
    return {'status': 'ready', 'threshold': threshold, 'validation': report,
            'calibration': metrics(cal_scores, cal_labels, threshold), 'duration_reports': duration_reports,
            'counts': counts, 'validation_ids': [r['id'] for r in validation],
            'verified': verified, 'validation_mode': validation_mode,
            'detail': '已训练并通过跨会话验证' if verified else '已训练，可输出参考分数；尚未通过充分的跨会话验证'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--result', required=True)
    args = parser.parse_args()
    try:
        result = train(args.snapshot, args.output)
    except Exception as exc:
        result = {'status': 'error', 'detail': f'{type(exc).__name__}: {exc}'}
    atomic_json(Path(args.result), result)


if __name__ == '__main__':
    main()
