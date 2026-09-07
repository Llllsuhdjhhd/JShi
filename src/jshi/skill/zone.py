"""05 的"写场 skill"：把本轮回复与材料整理成下一份现场/片场（独立于回复调用）。

- 木头：产出整份 ``rewritten_context``（整份重写，D-009）。
- 人格（苏西坡/斯密斯）：产出 ``edit``（块级增/删/改），由程序再追加本轮输入与回复。
- 与 ``CognitionSkill``（回复调用）分开：本 skill 只写场，不产出回复、不做对象确认、不召回。

用法：``WriteZoneSkill(base_model)``，作为主流程的 ``write_zone``（仍是 ModelPort）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from jshi.models import ModelPort, ModelRequest, ModelResponse

from .base import Skill, SkillError, parse_json_object

# 木头写场 schema：整份重写。
WOOD_WRITE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "rewritten_context": {"type": "string"},
    },
}

# 木头写场指令：整份重写当前现场。
WOOD_WRITE_INSTRUCTION = '''
【写场 · 整份重写下一份现场】
本轮你要重写「现场」——它是下一轮匠石面对这段对话时的工作上下文，不是评价、不是写给读者。

你在 user 里看到：
- 【上一份现场】：上一轮写好的现场正文；
- 【本轮原话】：对方这一轮刚对你说的话；
- 【你的回应】：你这一轮说的、或做的；
- 【本轮新回忆】：这一轮新想起来的旧片段；
- 对方是谁：见 user 的【说话人】。

重写目标：
1. 在【上一份现场】的基础上，把本轮原话、你的回应、新回忆补进下一份现场；尽量保留已有内容，不要把它当成待推倒重写的草稿。结果是下一轮能接着面对的现场，不是对过去做总结。
2. 对方是谁、已有承诺（都在 system 里）不要因重写而改变：不改变对方是谁；不否掉、不改写承诺的实质；不把没发生的写成已经发生；回忆归属不清时不要安到对方头上。
3. 现场宜短，不要超过 {active_zone_chars} 字；有写法要求（{style_instruction}）就按它写，没有就按与回复一致的现场叙事口吻写。
'''


def _to_write_response(data: Mapping[str, Any], model: str) -> ModelResponse:
    """把模型 JSON 映射为写场 ``ModelResponse``（木头整份 / 人格 edit）。"""
    rewritten = str(data.get("rewritten_context") or "").strip()
    edits = tuple(
        item
        for item in (data.get("edit") or ())
        if isinstance(item, Mapping)
    )
    return ModelResponse(
        model=model,
        rewritten_context=rewritten,
        zone_edit=edits,
    )


class WriteZoneSkill(Skill[ModelResponse]):
    """05 写场 skill：独立产出下一份现场/片场，失败降级为空写场结果。

    用法：``WriteZoneSkill(base_model)``，作为主流程的 ``write_zone``（仍是 ModelPort）。
    """

    name = "write_zone"
    instruction = WOOD_WRITE_INSTRUCTION
    schema: Mapping[str, Any] = WOOD_WRITE_SCHEMA

    def __init__(
        self,
        model: ModelPort,
        *,
        version: str = "v1",
    ) -> None:
        super().__init__(model, version=version)

    def parse(self, data: Mapping[str, Any]) -> ModelResponse:
        return _to_write_response(data, model=self.model_tag)

    def run(self, request: ModelRequest) -> ModelResponse:
        req = replace(request, system_extra=self.system_extra(request))
        raw = self._model.generate(req)
        raw_text = raw.text or ""
        try:
            data = parse_json_object(raw_text)
        except SkillError:
            return self._fallback(raw_text)
        response = self.parse(data).with_raw(raw_text)
        return response

    def _fallback(self, raw_text: str) -> ModelResponse:
        # 写场解析失败：交空写场结果（木头沿用上一份 / 人格片场不动），不挡回复。
        meta: dict[str, str] = {"skill_fallback": "write_zone_non_json"}
        preview = (raw_text or "")[:120]
        if preview:
            meta["raw_preview"] = preview
        return ModelResponse(
            model=self.model_tag,
            metadata=meta,
            rewritten_context="",
            zone_edit=(),
        )
