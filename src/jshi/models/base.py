from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.request import Request, urlopen

from jshi.core import SubjectState
from jshi.experienceledger.port import ContextAssessment


@dataclass(frozen=True)
class ModelSpeaker:
    object_id: str
    label: str
    aliases: tuple[str, ...] = ()
    status: str = "provisional"


@dataclass(frozen=True)
class ModelRequest:
    purpose: str
    input_text: str
    subject_state: SubjectState
    speaker: ModelSpeaker | None = None
    context: tuple[Mapping[str, Any], ...] = ()
    # skill 注入的系统级说明（角色锚定 + 输出 schema + 示例）；由适配器并入 system。
    system_extra: str = ""


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


@dataclass(frozen=True, init=False)
class ModelResponse:
    model: str
    metadata: Mapping[str, Any] | None = None
    response_plan: ResponsePlan = field(default_factory=lambda: ResponsePlan(mode="respond"))
    recall_requests: tuple[RecallRequest, ...] = ()
    object_assessment: ObjectAssessment | None = None
    context_assessment: ContextAssessment = field(default_factory=ContextAssessment)
    importance_ranking: tuple[ImportanceRank, ...] = ()

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
            "recall_requests": [],
        }
        return ModelResponse(
            text=json.dumps(payload, ensure_ascii=False),
            model=self.name,
        )


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
        personal_context = json.dumps(
            request.context, ensure_ascii=False, default=str
        )
        # 说话人身份必须明确：名字、称呼、object_id、状态单列，让模型清晰看到"谁在说 + 其 id"。
        speaker_line = ""
        if request.speaker is not None:
            alias_text = "、".join(request.speaker.aliases)
            speaker_line = (
                f"说话人：{request.speaker.label}"
                f"（称呼：{alias_text or '无'}；object_id={request.speaker.object_id}；"
                f"状态={request.speaker.status}）\n"
            )
        # 对象身份未确认时，把回应引导为澄清式提问（轻量路径，见 design/01 §4.3、05）。
        if request.speaker is not None and request.speaker.status != "confirmed":
            clarity_hint = (
                "\n注意：当前说话人身份尚未确认（provisional）。"
                "若需要向对方确认身份，请把你的回应写成一句澄清式提问"
                "（例如「你是……吗？」），不要默认身份成立。"
            )
        else:
            clarity_hint = ""
        system = (
            # skill 的系统级说明优先（角色锚定 + 输出 schema + 示例），再补主体背景。
            f"{request.system_extra}\n"
            f"{speaker_line}"
            f"{request.subject_state.identity_summary}\n"
            f"当前立场：{request.subject_state.current_stance}\n"
            f"重要价值：{', '.join(request.subject_state.salient_values)}\n"
            f"已有承诺：{', '.join(request.subject_state.commitments)}\n"
            f"相关背景材料：{personal_context}\n"
            "这些内容属于当前匠石的个人历史和认知处境。"
            "不要把推断或想象写成已经发生的事实。"
            "请区分事实、推断、反思和想象。"
            + clarity_hint
        )
        payload = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": request.input_text},
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
