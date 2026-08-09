from __future__ import annotations

from typing import Protocol


class ResultFeedbackPort(Protocol):
    """行动结果反馈：行动之后外部结果必须返回并影响未来。"""

    def ingest_result(
        self, subject_id: str, activity_id: str, action_text: str
    ) -> None: ...


class PlaceholderResultFeedback:
    """占位实现：接收结果但不做任何事（未实现）。"""

    name = "placeholder-feedback"

    def ingest_result(
        self, subject_id: str, activity_id: str, action_text: str
    ) -> None:
        return None
