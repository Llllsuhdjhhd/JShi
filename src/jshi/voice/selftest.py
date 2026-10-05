"""Labeled "try it" recordings for one voiceprint model. Never enrolls or changes thresholds."""
from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from time import time

from .comparison import calibrate

LIMIT = 60


def decide(ranking, threshold, margin):
    if not ranking or ranking[0][0] < threshold:
        return None
    if len(ranking) > 1 and ranking[0][0] - ranking[1][0] < margin:
        return None
    return ranking[0][1]


def recommend(calibration):
    if calibration.get('status') != 'ok':
        return None
    if calibration['strict_miss_rate'] <= .2:
        value, basis = calibration['strict_threshold'], 'no_false_accept'
    else:
        value, basis = calibration['eer_threshold'], 'balanced'
    return {'threshold': round(min(.95, max(.30, value)), 2), 'basis': basis,
            'miss_rate': calibration['strict_miss_rate'], 'error_rate': calibration['eer'],
            'reliable': calibration['reliable']}


class SelfTest:
    """Stores embeddings, not audio, so results are re-scored against the current bank."""
    def __init__(self, path: Path):
        self.path = path
        self.lock = RLock()
        self.trials = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else []

    def record(self, expected: str, vector) -> None:
        with self.lock:
            self.trials = [*self.trials, {'expected': expected, 'vector': list(map(float, vector)), 'at': time()}][-LIMIT:]
            self._save()

    def clear(self) -> None:
        with self.lock:
            self.trials = []
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.trials), encoding='utf-8')
        temp.replace(self.path)

    def summary(self, speakers) -> dict:
        counts = {'trials': 0, 'correct': 0, 'missed': 0, 'wrong': 0}
        genuine, impostor = [], []
        with self.lock:
            trials = list(self.trials)
        for trial in trials:
            ranking = speakers.rank_vector(trial['vector'])
            registered = {object_id for _, object_id in ranking}
            expected = trial['expected'] if trial['expected'] in registered else ''
            matched = decide(ranking, speakers.threshold, speakers.margin)
            counts['trials'] += 1
            if matched == expected or (not expected and matched is None):
                counts['correct'] += 1
            elif matched is None:
                counts['missed'] += 1
            else:
                counts['wrong'] += 1
            for score, object_id in ranking:
                (genuine if object_id == expected else impostor).append(score)
        calibration = calibrate(genuine, impostor)
        return {**counts, 'threshold': speakers.threshold, 'margin': speakers.margin,
                'calibration': calibration, 'recommendation': recommend(calibration)}
