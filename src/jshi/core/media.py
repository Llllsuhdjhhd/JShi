"""Media-neutral envelope and source-clock mapping; no speaker requirement."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class MediaEnvelope:
    source: str
    kind: str
    captured_at: float
    received_at: float
    reference: str = ''
    source_time: float | None = None
    clock_offset: float = 0
    clock_uncertainty: float = 0

    def __post_init__(self):
        if not self.source or self.kind not in {'image', 'video', 'audio', 'text'}:
            raise ValueError('invalid media envelope')
        values = (self.captured_at, self.received_at, self.clock_offset, self.clock_uncertainty)
        if any(not math.isfinite(x) for x in values) or self.clock_uncertainty < 0:
            raise ValueError('invalid media clock')


def map_source_time(source_time, offset=0):
    if not math.isfinite(source_time) or not math.isfinite(offset):
        raise ValueError('invalid source clock')
    return source_time + offset
