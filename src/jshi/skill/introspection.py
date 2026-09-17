"""自省 skill：把现场喂给模型，产出六问（⑤ 三个出口）。

对齐《doc/design/300-自省.md》§4.2 与《概念词汇》「工具」：

- 六问 = 什么 / 我的选择与结果 / 为什么（归因）/ 我是否可以做得更好 /
  以后我应该怎么做（⑤a 条件—动作、⑤b 要不要用工具、⑤c 要不要与某个对象进一步交流）/
  是否值得记下来并学习；
- **步骤不减、深度可变**：低档不调模型时，由调用方填 ``NOT_EVALUATED`` 占位；
- 结构化解析失败 → 降级为原文（``degraded``），不阻断、不重试。
"""

from __future__ import annotations

from typing import Any, Mapping

from jshi.reflection.port import IntrospectionAnswer

from .base import Skill

INTROSPECTION_INSTRUCTION = """你在做一次自省：回头看看自己做过的事。给你的"现场"由三部分组成——
当时的上下文、那件事前后的现场、以及从记忆里回溯到的相关片段。

按六问回答，用第一人称、具体、不空泛：

① what：发生了什么；先把现场的背景说清（对象、场景、前因、当时看见的材料）
② choice_and_result：我当时怎么选的、结果如何；参数/取值也套进这个语境（当时取什么值、效果如何）
③ why（归因）：分开写"我能控制的 / 环境的 / 偶然的"，再补一句"如果是别人做的，我会怎么归因"
④ better：我哪里可以做得更好、代价是什么；不空泛自夸或自责
⑤a next_time：以后我应该怎么做，写成"什么情形下做什么"
⑤b 要不要借助外部工具：只有"离开主体才办得到、有代价、经 200"才算工具（叫模型、召回记忆、
   装载个人世界都不算）。要就写清 tool_capability / tool_when / tool_worth
⑤c 要不要与某个对象进一步交流（道歉 / 回话 / 解释 / 确认）：要就写清对谁、要点、什么时候
⑥ worth_learning：是否值得记下来并学习；值得就把要点放进 candidates

材料不足就直说"材料不足"，不要编；不要写"我学到了很多"这类空话。"""

INTROSPECTION_JSON_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "what": {"type": "string"},
        "choice_and_result": {"type": "string"},
        "why": {"type": "string"},
        "better": {"type": "string"},
        "next_time": {"type": "string"},
        "tool_need": {"type": "boolean"},
        "tool_capability": {"type": "string"},
        "tool_when": {"type": "string"},
        "tool_worth": {"type": "string"},
        "interaction_need": {
            "type": "object",
            "properties": {
                "object_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": [
                        "apologize",
                        "reply",
                        "explain",
                        "confirm",
                        "ask",
                        "none",
                    ],
                },
                "gist": {"type": "string"},
                "when": {"type": "string"},
            },
            "required": ["kind"],
        },
        "worth_learning": {"type": "boolean"},
        "candidates": {"type": "array", "items": {"type": "object"}},
    },
    "required": [
        "what",
        "choice_and_result",
        "why",
        "better",
        "next_time",
        "worth_learning",
    ],
}


class IntrospectionSkill(Skill[IntrospectionAnswer]):
    name = "introspection"
    instruction = INTROSPECTION_INSTRUCTION
    schema = INTROSPECTION_JSON_SCHEMA

    def parse(self, data: Mapping[str, Any]) -> IntrospectionAnswer:
        interaction = data.get("interaction_need")
        if isinstance(interaction, Mapping):
            kind = _text(interaction.get("kind"))
            interaction = None if not kind or kind == "none" else dict(interaction)
        else:
            interaction = None
        candidates = tuple(
            dict(item)
            for item in (data.get("candidates") or ())
            if isinstance(item, Mapping)
        )
        return IntrospectionAnswer(
            what=_text(data.get("what")),
            choice_and_result=_text(data.get("choice_and_result")),
            why=_text(data.get("why")),
            better=_text(data.get("better")),
            next_time=_text(data.get("next_time")),
            tool_need=bool(data.get("tool_need")),
            tool_capability=_text(data.get("tool_capability")),
            tool_when=_text(data.get("tool_when")),
            tool_worth=_text(data.get("tool_worth")),
            interaction_need=interaction,
            worth_learning=bool(data.get("worth_learning")),
            candidates=candidates,
            mode="model",
        )

    def _fallback(self, raw_text: str) -> IntrospectionAnswer:
        """解析失败：保留原文，标降级；六问未评，不编。"""
        return IntrospectionAnswer(
            what="（结构化输出解析失败）",
            raw_text=raw_text or "",
            degraded=True,
            mode="fallback",
        )


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()
