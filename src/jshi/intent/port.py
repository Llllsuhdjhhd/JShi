from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from jshi.subject.domain import Activity


class IntentPort(Protocol):
    """意图系统：意图应当能够暂停、继续、完成或放弃并保留原因。"""

    def begin(self, activity: Activity) -> None:
        """活动建立时挂接意图系统。"""

    def resolve(
        self,
        activity: Activity,
        input_text: str,
        concern_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """根据输入与归属解析本活动的意图 id 集合；占位返回空。"""


class PlaceholderIntent:
    """占位实现：不记录任何意图（字段已有，未填充）。"""

    name = "placeholder-intent"

    def begin(self, activity: Activity) -> None:
        return None

    def resolve(
        self,
        activity: Activity,
        input_text: str,
        concern_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        return ()
