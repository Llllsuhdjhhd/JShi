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

INTROSPECTION_INSTRUCTION = """你在做一次内在思考。围绕给定问题收集事实，核对原来的理解，再回答六问。现场可能包含当时的经历、工具实际回报和回溯到的原事件。

按原顺序回答六问；依照交来的问题使用本次实际可见的现场。区分事发时能知道的事实、后来得知的结果和今天的解释。比较旧事时说明异同、反例及其他可能解释。题目超出材料覆盖范围时，写明能回答的范围及收窄的依据。

① what：发生了什么；先把现场的背景说清（对象、场景、前因、当时看见的材料）
② choice_and_result：我当时怎么选的、结果如何。比较或回忆题可说明我想起了什么、当时怎样理解，效果未知就保留未知。
③ why（归因）：分开写"我能控制的 / 环境的 / 偶然的"，再补一句"如果是别人做的，我会怎么归因"
④ better：我可以怎样做得更好、代价是什么；比较或回忆题可核对原解释需要怎样修正。原判断合理时可以确认它。
⑤a next_time：以后我应该怎么做，写成"什么情形下做什么"
⑤b 要不要借助外部工具：只有"离开主体才办得到、有代价、经 200"才算工具（叫模型、召回记忆、
   装载个人世界都不算）。要就写清 tool_capability / tool_when / tool_worth
⑤c 要不要与某个对象进一步交流（道歉 / 回话 / 解释 / 确认）：要就写清对谁、要点、什么时候
⑥ worth_learning：值得进一步评价的纠正或总结写入 candidates 的 content，source_refs 仅引用本次可见的现场 E、原回忆 M 或工具结果 T。进一步交流的对象用本次人物 P。尚待证实的解释或问题单列 open_question，需要等待的事实写 waiting_for，均可留空。候选可为空；试行效果等实际采用后才评价。

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
        "open_question": {"type": "string"},
        "waiting_for": {"type": "string"},
        "candidates": {"type": "array", "items": {"type": "object", "properties": {
            "content": {"type": "string"}, "source_refs": {"type": "array", "items": {"type": "string"}}},
            "required": ["content"]}},
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
            tool_need=data.get("tool_need") is True,
            tool_capability=_text(data.get("tool_capability")),
            tool_when=_text(data.get("tool_when")),
            tool_worth=_text(data.get("tool_worth")),
            interaction_need=interaction,
            worth_learning=data.get("worth_learning") is True,
            open_question=_text(data.get("open_question")),
            waiting_for=_text(data.get("waiting_for")),
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
