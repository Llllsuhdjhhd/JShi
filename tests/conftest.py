from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def _pin_memory_control_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the in-process memory-control default clock to noon UTC.

    ``SubjectProcess`` builds ``InProcessMemoryControl`` with the default
    ``now=utc_now`` and ``flush_night_window="00:00-06:00"``. When the suite runs
    inside that UTC window (morning in Asia/Shanghai), a single ``experience`` is
    flushed immediately and records extra ``memory_external`` facts, making
    otherwise deterministic assertions time-dependent.

    Night-window and idle tests that care about the clock already pass an explicit
    ``now``, so they are unaffected.
    """
    fixed = lambda: datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("jshi.memorycontrol.inprocess.local_now", fixed)
