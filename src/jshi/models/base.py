from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from urllib.request import Request, urlopen

from jshi.core import SubjectState
from jshi.experienceledger.port import ContextAssessment


@dataclass(frozen=True)
class ModelSpeaker:
    object_id: str
    label: str
    aliases: tuple[str, ...] = ()
    status: str = "provisional"
    reason: str = ""


@dataclass(frozen=True)
class ModelRequest:
    purpose: str
    input_text: str
    subject_state: SubjectState
    speaker: ModelSpeaker | None = None
    context: tuple[Mapping[str, Any], ...] = ()
    # skill 注入的系统级说明（角色锚定 + 输出 schema + 示例）；由适配器并入 system。
    system_extra: str = ""
    # 本轮“现在”时刻；渲染提示词时给出相对时间锚点（如“昨天”）。
    now: datetime | None = None
    # 超级权限用户写入的附加规则（已格式化），渲染进 system 的【附加规则】。
    governing_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecallRequest:
    """追加召回请求：认知活动发现一次装载不够时由模型结构化提出。"""

    query: str
    budget: int = 3
    object_ids: tuple[str, ...] = ()
    anchor_event_ids: tuple[str, ...] = ()
    level: int = 1  # 回忆档位 1–9（09 未实现档位语义，本期占位记录）


@dataclass(frozen=True)
class ResponseItem:
    channel: str  # verbal | embodied
    text: str = ""


@dataclass(frozen=True)
class ResponsePlan:
    mode: str  # respond | think | ignore | wait
    reason: str = ""
    items: tuple[ResponseItem, ...] = ()

    def verbal_text(self) -> str:
        if self.mode in {"think", "ignore", "wait"}:
            return ""
        for item in self.items:
            if item.channel == "verbal" and item.text.strip():
                return item.text
        return ""

    def has_embodied(self) -> bool:
        return any(item.channel == "embodied" for item in self.items)

    def embodied_text(self) -> str:
        for item in self.items:
            if item.channel == "embodied" and item.text.strip():
                return item.text
        return ""


@dataclass(frozen=True)
class ObjectAssessment:
    """认知阶段（阶段⑤）对说话人候选的判定。"""

    conclusion: str  # confirm | deny | uncertain
    object_id: str | None = None
    label: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class RecallEvaluation:
    """追加召回后、下一轮响应中对上一轮回忆的评价（程序容忍缺失）。"""

    usefulness: str  # related | partial | unrelated
    redundant: bool = False
    need_more: bool = False
    level_feedback: str = "ok"  # too_low | ok | too_high
    note: str = ""


@dataclass(frozen=True)
class ImportanceRank:
    """05 的第 5 用途段"重要性排序"：给 14 统计用（当前占位，14 未接管）。"""

    id: str
    importance: float = 0.0
    reason: str = ""


    reason: str = ""


@dataclass(frozen=True)
class MemoryRating:
    """对一条已在场回忆的现场打分。"""

    ref: str
    relevance: str = "unrelated"
    helps_understanding: int = 0
    used_in_reply: str = "unused"
    misleading: bool = False
    redundant: bool = False
    object_fit: str = "none"


@dataclass(frozen=True)
class MemoryRatings:
    """本轮对已在场回忆的整轮打分；无在场回忆则为空。"""

    items: tuple[MemoryRating, ...] = ()
    coverage: str = ""
    gap_query: str = ""

    def has_content(self) -> bool:
        return bool(self.items or self.coverage or (self.gap_query or "").strip())


@dataclass(frozen=True, init=False)
class ModelResponse:
    model: str
    metadata: Mapping[str, Any] | None = None
    response_plan: ResponsePlan = field(default_factory=lambda: ResponsePlan(mode="respond"))
    recall_requests: tuple[RecallRequest, ...] = ()
    object_assessment: ObjectAssessment | None = None
    context_assessment: ContextAssessment = field(default_factory=ContextAssessment)
    importance_ranking: tuple[ImportanceRank, ...] = ()
    memory_ratings: MemoryRatings = field(default_factory=MemoryRatings)

    @property
    def text(self) -> str:
        for item in self.response_plan.items:
            if item.channel == "verbal":
                return item.text
        return ""

    @property
    def response_statuses(self) -> tuple[str, ...]:
        statuses = [self.response_plan.mode]
        for item in self.response_plan.items:
            if item.channel not in statuses:
                statuses.append(item.channel)
        return tuple(statuses)

    def __init__(
        self,
        model: str,
        metadata: Mapping[str, Any] | None = None,
        response_plan: ResponsePlan | None = None,
        recall_requests: tuple[RecallRequest, ...] = (),
        object_assessment: ObjectAssessment | None = None,
        context_assessment: ContextAssessment | None = None,
        importance_ranking: tuple[ImportanceRank, ...] = (),
        memory_ratings: MemoryRatings | None = None,
        *,
        text: str | None = None,
        response_statuses: tuple[str, ...] = (),
    ) -> None:
        if response_plan is None:
            items: list[ResponseItem] = []
            if text:
                items.append(ResponseItem(channel="verbal", text=text))
            if "embodied" in response_statuses and not any(
                item.channel == "embodied" for item in items
            ):
                items.append(ResponseItem(channel="embodied", text=""))
            mode = "respond" if items else "think"
            response_plan = ResponsePlan(mode=mode, items=tuple(items))

        object.__setattr__(self, "model", model)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "response_plan", response_plan)
        object.__setattr__(self, "recall_requests", recall_requests)
        object.__setattr__(self, "object_assessment", object_assessment)
        object.__setattr__(
            self,
            "context_assessment",
            context_assessment if context_assessment is not None else ContextAssessment(),
        )
        object.__setattr__(self, "importance_ranking", importance_ranking)
        object.__setattr__(
            self,
            "memory_ratings",
            memory_ratings if memory_ratings is not None else MemoryRatings(),
        )


class ModelPort(Protocol):
    @property
    def name(self) -> str: ...

    def generate(self, request: ModelRequest) -> ModelResponse: ...


class EchoModel:
    """Offline deterministic model for framework tests and smoke runs."""

    name = "echo"

    def generate(self, request: ModelRequest) -> ModelResponse:
        if request.purpose in {"inner", "reflection"}:
            return ModelResponse(
                text=f"我正在回顾：{request.input_text}",
                model=self.name,
            )
        payload = {
            "response_plan": {
                "mode": "respond",
                "reason": "offline echo",
                "items": [
                    {
                        "channel": "verbal",
                        "text": f"我听见了：{request.input_text}",
                    }
                ],
            },
            "context_assessment": {"remove": [], "drop_recall": [], "focus": []},
        }
        return ModelResponse(
            text=json.dumps(payload, ensure_ascii=False),
            model=self.name,
        )


def iter_top_level_json_values(chunks: Iterator[str]) -> Iterator[tuple[str, str]]:
    """边到边地提取 JSON 对象顶层成员。

    ``chunks`` 按序产出原始文本(流式 content 分片)。每有一个顶层 ``key: value`` 完整
    到达就 ``yield (key, value_json)``。用标准 ``raw_decode`` 逐成员解析,自然处理字符串、
    转义、嵌套对象/数组与标量。容忍 ``{`` 之前的 ```json 围栏/空白以及对象闭合后的余文。
    """

    buffer = ""
    decoder = json.JSONDecoder()
    idx = 0
    root_open_at: int | None = None
    pending_key: str | None = None
    stage = 0  # 0 expect key, 1 expect colon, 2 expect value

    for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk
        if root_open_at is None:
            start = buffer.find("{")
            if start < 0:
                continue
            root_open_at = start
            idx = root_open_at + 1
        while True:
            if stage == 0:
                while idx < len(buffer) and buffer[idx] in " \t\r\n,":
                    idx += 1
                if idx >= len(buffer):
                    break
                if buffer[idx] == "}":
                    return  # root object closed
                if buffer[idx] != '"':
                    break
                try:
                    key, key_end = decoder.raw_decode(buffer, idx)
                except json.JSONDecodeError:
                    break  # key 未完整,等更多分片
                pending_key = key
                idx = key_end
                stage = 1
                continue
            if stage == 1:
                while idx < len(buffer) and buffer[idx] in " \t\r\n":
                    idx += 1
                if idx >= len(buffer):
                    break
                if buffer[idx] != ":":
                    break
                idx += 1
                stage = 2
                continue
            # stage == 2: expect value
            while idx < len(buffer) and buffer[idx] in " \t\r\n":
                idx += 1
            if idx >= len(buffer):
                break
            try:
                _value, value_end = decoder.raw_decode(buffer, idx)
            except json.JSONDecodeError:
                break  # value 尚未完整,等更多分片(stage 仍为 2)
            yield (pending_key, buffer[idx:value_end])
            pending_key = None
            stage = 0
            idx = value_end


def _verbal_text(plan: Mapping[str, Any]) -> str:
    """从 ``response_plan`` 段取口头文本;非 respond 或空则返回空串。"""
    mode = (plan.get("mode") or "").strip()
    if mode not in {"respond"}:
        return ""
    for item in plan.get("items") or ():
        if isinstance(item, Mapping) and item.get("channel") == "verbal":
            text = (item.get("text") or "").strip()
            if text:
                return text
    return ""


class OpenAICompatibleModel:
    """Minimal adapter for providers exposing an OpenAI-compatible chat endpoint."""

    def __init__(self, endpoint: str, api_key: str, model: str) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return self._model

    def generate(self, request: ModelRequest) -> ModelResponse:
        from jshi.models.prompt import build_system, build_user

        payload = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": build_system(request)},
                    {"role": "user", "content": build_user(request)},
                ],
            }
        ).encode("utf-8")
        http_request = Request(
            self.endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(http_request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
        return ModelResponse(
            text=result["choices"][0]["message"]["content"],
            model=self.name,
            metadata={"provider_response_id": result.get("id")},
        )

    def generate_stream(self, request: ModelRequest) -> Iterator[str]:
        """流式产内容分片(`choices[0].delta.content`),供"边到边"消费。

        与 ``generate`` 同一套 system/user;只额外加 ``stream: True`` 并按 SSE
        (``data: {...}``)逐行读。对 ``[DONE]`` 终止。
        """
        from jshi.models.prompt import build_system, build_user

        payload = json.dumps(
            {
                "model": self._model,
                "stream": True,
                "messages": [
                    {"role": "system", "content": build_system(request)},
                    {"role": "user", "content": build_user(request)},
                ],
            }
        ).encode("utf-8")
        http_request = Request(
            self.endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(http_request, timeout=60) as response:
            for line in response:
                raw = line.decode("utf-8") if isinstance(line, bytes) else str(line)
                text = raw.strip()
                if not text or not text.startswith("data:"):
                    continue
                data = text[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content
