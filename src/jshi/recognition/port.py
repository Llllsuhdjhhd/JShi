from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SpeakerCandidate:
    """身份系统识别（对象识别）的输出：谁在说话。

    结果默认是待校验候选：未知/暂定对象须经确认机制才能升为已确认。
    """

    label: str = "human"          # 占位说话人标签
    object_id: str | None = None  # 对话对象标识；未识别时为 None
    confidence: float = 0.0
    status: str = "unknown"       # unknown | provisional | confirmed


class ObjectRecognitionPort(Protocol):
    """可替换的对象识别系统（渠道身份，区别于文本实体抽取）。"""

    def identify(self, subject_id: str, input_text: str) -> SpeakerCandidate:
        """识别当前输入来自哪个对话对象；输出默认为待校验候选。"""


class PlaceholderObjectRecognition:
    """占位实现：不识别，固定返回未知对象（human）。"""

    name = "placeholder-recognition"

    def identify(self, subject_id: str, input_text: str) -> SpeakerCandidate:
        return SpeakerCandidate()
