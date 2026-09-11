"""200 包装 skill：把一本 210 的阶段性事实写成给 03 的一句。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from jshi.models import ModelRequest
from jshi.tool.contract import FeedbackKind, ToolFeedback
from jshi.tool.hang import HangRecord

from .base import Skill
from .tool_io import tool_skill_request

TOOL_WRAP_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "visible": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": ["visible"],
}


@dataclass(frozen=True)
class WrapResult:
    visible: bool
    summary: str = ""


class ToolWrapSkill(Skill[WrapResult]):
    name = "tool_wrap"
    version = "v1"
    schema = TOOL_WRAP_SCHEMA
    instruction = """你把工具进行中的事实写成一句给匠石看的话，像他自己刚弄清的进展。
不是对那个人开口，不要称呼对方、不要写成 response_plan。
不要把引擎思维链、调试日志写进去。
没有可说的阶段性成果就 visible=false。
有可说的就一句，短，不要堆过程。
同一件事不要换个说法再报一遍。
任务 JSON 含 need、template、previous_summary、facts。"""

    def parse(self, data: Mapping[str, Any]) -> WrapResult:
        visible = bool(data.get("visible"))
        summary = str(data.get("summary") or "").strip()
        if visible and not summary:
            return WrapResult(False, "")
        return WrapResult(visible, summary)

    def _fallback(self, raw_text: str) -> WrapResult:
        del raw_text
        return WrapResult(False, "")

    def wrap_hang(
        self, hang: HangRecord, batch: Sequence[ToolFeedback]
    ) -> WrapResult:
        facts = []
        for item in batch:
            if item.kind is FeedbackKind.RESULT and item.result is not None:
                result = item.result
                facts.append(
                    {
                        "kind": "result",
                        "status": result.status.value,
                        "text": (result.summary or result.error or "").strip(),
                    }
                )
            elif item.kind is FeedbackKind.PROGRESS and item.progress is not None:
                text = (item.progress.partial or "").strip()
                if text:
                    facts.append({"kind": "progress", "text": text})
        request: ModelRequest = tool_skill_request(
            hang.subject_id,
            {
                "need": hang.need,
                "template": hang.template,
                "previous_summary": hang.summary,
                "facts": facts,
            },
        )
        return self.run(request)
