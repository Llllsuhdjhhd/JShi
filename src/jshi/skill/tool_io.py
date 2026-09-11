"""200 skill 共用：把窄 JSON 任务塞进 ModelRequest。"""

from __future__ import annotations

import json
from typing import Any, Mapping

from jshi.core import SubjectState
from jshi.models import ModelRequest


def tool_skill_request(subject_id: str, payload: Mapping[str, Any]) -> ModelRequest:
    return ModelRequest(
        purpose="tool_internal",
        input_text=json.dumps(payload, ensure_ascii=False),
        subject_state=SubjectState(
            subject_id=subject_id or "",
            identity_summary="",
            current_stance="",
        ),
    )
