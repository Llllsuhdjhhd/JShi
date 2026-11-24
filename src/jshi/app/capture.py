"""Exclusive live input ownership. All mutations run on the host event loop."""
from dataclasses import dataclass, field
from secrets import token_urlsafe


@dataclass(frozen=True)
class CaptureSource:
    kind: str
    device_id: str


DESKTOP = CaptureSource('desktop', 'local')


@dataclass(eq=False)
class CaptureSession:
    source: CaptureSource
    capture_id: str
    token: str = field(default_factory=lambda: token_urlsafe(24), repr=False)
    accepting: bool = False


class CaptureSources:
    def __init__(self):
        self.active: CaptureSession | None = None

    def acquire(self, source: CaptureSource, capture_id: str) -> CaptureSession:
        if self.active is not None:
            raise RuntimeError('已有采集设备连接，请先断开当前设备')
        self.active = CaptureSession(source, capture_id)
        return self.active

    def resolve(self, source: CaptureSource, token: str | None) -> CaptureSession | None:
        session = self.active
        if session and session.accepting and session.source == source and session.token == token:
            return session
        return None

    def release(self, session: CaptureSession) -> None:
        if self.active is session:
            session.accepting = False
            self.active = None
