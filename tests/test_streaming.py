"""I-001 流式:单 JSON 边到边消费,response_plan 完整即提前开口,其余段后台收。"""

from __future__ import annotations

from jshi.core import SubjectState
from jshi.models import ModelRequest, ModelResponse
from jshi.skill.cognition import CognitionSkill

JSON_TEXT = (
    '{"response_plan":{"mode":"respond","reason":"r",'
    '"items":[{"channel":"verbal","text":"你好呀"}]},'
    '"context_assessment":{"remove":[],"focus":[]},'
    '"memory_ratings":{"items":[],"coverage":"thin","gap_query":"lux"}}'
)


class FakeStreamingModel:
    name = "fake-stream"

    def __init__(self, text: str, chunk: int = 4) -> None:
        self._text = text
        self._chunk = chunk

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text=self._text, model=self.name)

    def generate_stream(self, request: ModelRequest):  # noqa: ANN201
        for i in range(0, len(self._text), self._chunk):
            yield self._text[i:i + self._chunk]


def _request() -> ModelRequest:
    return ModelRequest(
        purpose="subject_activity",
        input_text="你好",
        subject_state=SubjectState(subject_id="stone", identity_summary="", current_stance=""),
    )


def test_stream_emits_reply_early_and_returns_full():
    skill = CognitionSkill(FakeStreamingModel(JSON_TEXT))
    called: list[str] = []
    result = skill.run_stream(_request(), on_reply=lambda text: called.append(text))
    assert called == ["你好呀"]  # response_plan 完整即回调(提前开口)
    assert isinstance(result, ModelResponse)
    assert result.response_plan.mode == "respond"
    assert result.response_plan.verbal_text() == "你好呀"
    assert result.context_assessment is not None  # 其余段也全部消费
    assert result.memory_ratings.coverage == "thin"
    assert result.memory_ratings.gap_query == "lux"


def test_stream_mode_think_does_not_emit_reply():
    think = (
        '{"response_plan":{"mode":"think","reason":"内省",'
        '"items":[]},"context_assessment":{"remove":[],"focus":[]}}'
    )
    skill = CognitionSkill(FakeStreamingModel(think))
    called: list[str] = []
    skill.run_stream(_request(), on_reply=lambda text: called.append(text))
    assert called == []  # think 无口头,不提前开口


def test_stream_lacks_streaming_falls_back_to_full():
    class NonStreaming:
        name = "fake"

        def generate(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(text=JSON_TEXT, model=self.name)

    skill = CognitionSkill(NonStreaming())
    called: list[str] = []
    result = skill.run_stream(_request(), on_reply=lambda text: called.append(text))
    assert called == []  # 无流式 -> 退回 run,等整份,不提前回
    assert result.response_plan.verbal_text() == "你好呀"
