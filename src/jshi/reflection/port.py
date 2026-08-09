from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from jshi.subject.domain import CognitiveContent
    from jshi.subject.process import SubjectProcess


class ReflectionPort(Protocol):
    """反思与学习系统：回顾经历、继续未完成问题、重新理解旧记忆。"""

    def reflect(self, subject_id: str, prompt: str) -> CognitiveContent: ...


class PlaceholderReflection:
    """占位实现：委托给主体流程的当前最小反思逻辑。"""

    name = "placeholder-reflection"

    def __init__(self, process: "SubjectProcess") -> None:
        self._process = process

    def reflect(self, subject_id: str, prompt: str) -> CognitiveContent:
        return self._process._reflect_internal(subject_id, prompt)
