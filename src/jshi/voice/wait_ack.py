"""Measured waiting guidance; no extra model call or synthetic conversation facts."""
import json
import math
import os
from pathlib import Path
from statistics import median
from threading import RLock
from time import monotonic


class WaitAckTiming:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = RLock()
        self.last_ack = None
        try:
            self.rows = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.rows = {}
        if not isinstance(self.rows, dict):
            self.rows = {}
        self.rows = {key: [value for value in values if type(value) in (int, float)
                     and math.isfinite(value) and 0 < value <= 300][-20:]
                     for key, values in self.rows.items() if isinstance(values, list)}

    def observe(self, model, level, seconds):
        if not math.isfinite(seconds) or not 0 < seconds <= 300:
            return
        with self.lock:
            key = f'{model}:L{level}'
            self.rows[key] = [*self.rows.get(key, []), round(seconds, 2)][-20:]
            self.rows = dict(list(self.rows.items())[-32:])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.rows), encoding='utf-8')
            temp.replace(self.path)

    def guidance(self, model, *, allowed, mode='voice'):
        enabled = os.getenv('JSHI_JEV_WAIT_ACK_ENABLED', 'true').lower() not in {'0', 'false', 'off'}
        with self.lock:
            levels = {}
            for level, initial in ((1, 10), (2, 6), (3, 3)):
                samples = self.rows.get(f'{model}:L{level}', [])
                seconds = median(samples) if samples else initial
                duration = min(3, max(0, seconds - 1))
                levels[f'L{level}'] = {'expected_seconds': round(seconds, 1), 'samples': len(samples),
                    'basis': 'recent_median' if samples else 'initial_estimate',
                    'suggested_speech_seconds': round(duration, 1),
                    'recommendation': 'brief_ack' if seconds >= 3 else 'optional' if seconds >= 1.5 else 'silent',
                    'max_chars': min(16, int(duration * 4))}
            cooldown_ready = self.last_ack is None or monotonic() - self.last_ack >= 30
        return {'allowed': bool(allowed and enabled and cooldown_ready), 'mode': mode, 'levels': levels,
                'note': '耗时为预计值，字数按暂定每秒4字符估计；不保证填满等待时间，不为凑时长扩写。'}

    def claim_ack(self):
        """Do not insert acknowledgements on every consecutive turn."""
        with self.lock:
            if os.getenv('JSHI_JEV_WAIT_ACK_ENABLED', 'true').lower() in {'0', 'false', 'off'}:
                return False
            now = monotonic()
            if self.last_ack is not None and now - self.last_ack < 30:
                return False
            self.last_ack = now
            return True


def wait_timing(process):
    if not hasattr(process, '_jev_wait_ack_timing'):
        process._jev_wait_ack_timing = WaitAckTiming(process.repository.path.parent / 'jev_wait_ack_timings.json')
    return process._jev_wait_ack_timing


def model_key(process):
    model = process.cognition
    return str(getattr(model, 'model_tag', getattr(model, 'name', type(model).__name__)))


def ack_context(text, *, channel='voice'):
    """A provisional utterance is neither a user instruction nor a delivery fact."""
    status = '已安排语音，未确认播出，可能跳过' if channel == 'voice' else '已提交页面显示，未确认已读'
    return f'【本轮过渡语】{status}：{json.dumps(text, ensure_ascii=False)}。这是匠石的预先接话，不是用户要求或执行结果；无论尚待播出还是已播，都不另写语义相同的确认或执行承诺；正式回应提供新增结果、必要澄清或明确变化。尚无结果时继续实际处理，不再仅说一遍“我查一下”等承诺；不能把预先接话当作工具已执行。确需纠正时简短说明。'
