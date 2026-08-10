from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.request import Request, urlopen

from jshi.core import SubjectState


@dataclass(frozen=True)
class ModelRequest:
    purpose: str
    input_text: str
    subject_state: SubjectState
    context: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class RecallRequest:
    """追加召回请求：认知活动发现一次装载不够时由模型结构化提出。"""

    query: str
    budget: int = 3
    object_ids: tuple[str, ...] = ()
    anchor_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObjectAssessment:
    """认知阶段（阶段⑤）对说话人候选的判定。"""

    conclusion: str  # confirm | deny | uncertain
    object_id: str | None = None
    label: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str
    metadata: Mapping[str, Any] | None = None
    recall_requests: tuple[RecallRequest, ...] = ()
    object_assessment: ObjectAssessment | None = None


class ModelPort(Protocol):
    @property
    def name(self) -> str: ...

    def generate(self, request: ModelRequest) -> ModelResponse: ...


class EchoModel:
    """Offline deterministic model for framework tests and smoke runs."""

    name = "echo"

    def generate(self, request: ModelRequest) -> ModelResponse:
        if request.purpose in {"inner", "reflection"}:
            text = f"我正在回顾：{request.input_text}"
        else:
            text = f"我听见了：{request.input_text}"
        return ModelResponse(text=text, model=self.name)


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
        system = (
            f"{request.subject_state.identity_summary}\n"
            f"当前立场：{request.subject_state.current_stance}\n"
            f"重要价值：{', '.join(request.subject_state.salient_values)}\n"
            f"未完成现实（主体面）：{', '.join(request.subject_state.concerns)}\n"
            f"已有承诺：{', '.join(request.subject_state.commitments)}\n"
            f"相关个人世界：{personal_context}\n"
            "这些内容属于当前匠石的个人历史和认知处境。"
            "不要把推断或想象写成已经发生的事实。"
            "请区分事实、推断、反思和想象。"
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
