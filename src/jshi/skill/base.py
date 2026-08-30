"""skill 框架：模型驱动的"结构化能力"。

一个 skill = 一个模型（``ModelPort``）+ 结构化 JSON schema + 解析 + 版本。
它把"调模型 → 出 JSON → 解析校验 → 失败降级 → 记版本"这套公共机制统一起来，
任何需要"让模型产结构化结果"的能力（认知、对象确认、反思、边界判断、评价等）
都可以做成一个 skill 子类，而不是各自重写一套。

对齐：
- I-004：skill / 模型 / 工具都是"能力实现"，按阶段按需选用；每个 skill 有版本、
  能力边界；每次活动记录实际使用的模型与 skill 版本（``model_tag``）。
- I-001：一次调用可同时产出多个用途段（如 ``response_plan`` / ``context_assessment``），
  消费方拿到哪段就处理哪段。

本项目约定：
- 对外仍复用 ``ModelPort.generate(ModelRequest) -> ModelResponse``，主流程接口不变。
- skill 的输入统一是 ``ModelRequest``（子类决定其中 ``subject_state`` / ``context`` /
  ``speaker`` 等怎么填）；框架把 ``request.input_text`` 包装成"instruction + schema + 任务"
  提示词后调模型。
- 结果仍是候选；解析/校验失败时走子类的降级策略（默认抛 ``SkillError``）。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Any, Generic, Mapping, TypeVar

from jshi.core.params import active_zone_chars
from jshi.models import ModelPort, ModelRequest, ModelResponse

T = TypeVar("T")


class SkillError(RuntimeError):
    """skill 结构化输出解析 / 校验失败。"""


def _strip_markdown_fences(raw: str) -> str:
    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _first_json_object(text: str) -> str:
    """从带前后缀的文本里取出第一个配平的 ``{...}``；没有花括号就原样返回。"""
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return text[start:]


def parse_json_object(text: str) -> Mapping[str, Any]:
    """把模型返回文本解析为 JSON 对象；容忍 Markdown 围栏与前后缀，失败抛 ``SkillError``。"""
    raw = (text or "").strip()
    stripped = _strip_markdown_fences(raw)
    for candidate in (raw, stripped, _first_json_object(stripped)):
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    raise SkillError(f"non-JSON skill output: {raw[:120]!r}")


class Skill(ABC, Generic[T]):
    """模型驱动"能力"基类：输入 ``ModelRequest``，输出结构化 ``T``。

    子类提供：

    - ``name``：能力名（如 ``cognition``）；
    - ``instruction``：任务说明（用模型的中文指令）；
    - ``schema``：要求模型输出的 JSON Schema；
    - ``parse(data) -> T``：把模型 JSON 映射为结果；
    - （可选）``_fallback(raw_text)``：解析失败时的降级；默认抛 ``SkillError``。

    框架统一完成提示词组装、模型调用、JSON 解析、失败降级与版本记录。
    """

    name: str = ""
    instruction: str = ""
    schema: Mapping[str, Any] = {}

    def __init__(
        self,
        model: ModelPort,
        *,
        version: str = "v1",
        fallback: Any = None,
    ) -> None:
        self._model = model
        self.version = version
        self._fallback_value = fallback

    @property
    def model_tag(self) -> str:
        """"实际使用的模型 + skill 版本"，供活动记录（I-004）。"""
        return f"{self._model.name}@{self.version}"

    def system_extra(self, request: ModelRequest | None = None) -> str:
        """skill 注入 system 的规则与紧凑 JSON Schema。本轮材料由适配器放进 user。"""
        instruction = (
            self._render_instruction(request) if request is not None else self.instruction
        )
        schema_doc = json.dumps(
            self.schema, ensure_ascii=False, separators=(",", ":")
        )
        return (
            f"{instruction}\n"
            "请严格按下面的 JSON Schema 输出：只输出一个 JSON 对象，不要任何解释文字，"
            "枚举字段（mode / channel / conclusion 等）必须取枚举值。\n"
            f"JSON Schema：{schema_doc}"
        )

    def _render_instruction(self, request: ModelRequest) -> str:
        """把 instruction 里的 ``{speaker_label}`` 等占位符替换为当前轮真实值。"""
        speaker = request.speaker
        state = request.subject_state
        values = {
            "subject_id": state.subject_id,
            "speaker_label": speaker.label if speaker else "未知",
            "speaker_object_id": speaker.object_id if speaker else "",
            "speaker_status": speaker.status if speaker else "",
            "values": "\n".join(
                str(item).strip()
                for item in state.salient_values
                if str(item).strip()
            ),
            "active_zone_chars": str(active_zone_chars()),
        }
        text = self.instruction
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        return text

    @abstractmethod
    def parse(self, data: Mapping[str, Any]) -> T:
        """把模型 JSON 映射为结果类型 ``T``。"""

    def _fallback(self, raw_text: str) -> T:
        if self._fallback_value is not None:
            return self._fallback_value  # type: ignore[return-value]
        raise SkillError(f"skill '{self.name}' failed to produce structured output")

    def run(self, request: ModelRequest) -> T:
        """执行 skill：把角色/说明并入 system（system_extra），input_text 保持任务。"""
        req = replace(request, system_extra=self.system_extra(request))
        raw = self._model.generate(req)
        try:
            data = parse_json_object(raw.text)
        except SkillError:
            return self._fallback(raw.text)
        return self.parse(data)


class SkillModelPort(ModelPort):
    """把 ``Skill[ModelResponse]`` 包装成 ``ModelPort``（主流程只认 generate）。

    - ``apply_to`` 之外的 purpose（如 reflection）走底层裸模型，避免对话 schema
      污染内部活动输出；
    - ``name`` 使用 ``model_tag``（模型 + skill 版本），供 I-004 活动记录。
    """

    def __init__(
        self,
        skill: Skill[ModelResponse],
        *,
        apply_to: tuple[str, ...] = ("subject_activity",),
    ) -> None:
        self._skill = skill
        self._apply_to = apply_to

    @property
    def name(self) -> str:
        return self._skill.model_tag

    def generate(self, request: ModelRequest) -> ModelResponse:
        if request.purpose in self._apply_to:
            return self._skill.run(request)
        return self._skill._model.generate(request)


class SkillRegistry:
    """按能力名登记/选取 skill（I-004：各阶段按需选用能力实现）。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"skill already registered: {skill.name}")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)
