"""工具使用模块端口：可替换引擎 + 编排模块。

- ``ToolEngine``：工具使用引擎（可替换）。工具目录 / 包管理由引擎负责
  （Pi 用 ``get_commands`` / skills / packages），匠石侧不维护自己的注册表。
- ``ToolModule``：收 ``ToolRequest``，经引擎跑，返回反馈流 + 终态结果。
- ``ToolRunner``：后台跑执行，把反馈**逐条**写入记挂；失败写成 ``result/failed``。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Protocol, TYPE_CHECKING

from .contract import FeedbackKind, ToolFeedback, ToolRequest, ToolResult, ToolStatus

if TYPE_CHECKING:
    from .hang import HangStore

logger = logging.getLogger(__name__)


class ToolEngine(Protocol):
    """工具使用引擎（可替换）。目录 / 安装 / 执行归引擎。"""

    name: str

    def list_templates(self) -> tuple[str, ...]:
        """返回可用工具模板清单。真接 Pi 后来自 ``get_commands`` / skills。"""
        ...

    def execute(self, request: ToolRequest) -> tuple[ToolFeedback, ...]:
        """执行一次工具使用，返回反馈流（progress / result）。

        引擎不 yield estimate；报价由 200/205 写入记挂。
        引擎只产出过程反馈与候选结果，不写匠石长期状态（D-004）。
        """
        ...


@dataclass(frozen=True)
class ToolUseOutcome:
    """一次工具使用的完整结果。"""

    request_id: str
    feedback: tuple[ToolFeedback, ...]
    result: ToolResult

    @property
    def estimate(self) -> ToolFeedback | None:
        for item in self.feedback:
            if item.kind is FeedbackKind.ESTIMATE:
                return item
        return None

    @property
    def progress(self) -> tuple[ToolFeedback, ...]:
        return tuple(item for item in self.feedback if item.kind is FeedbackKind.PROGRESS)

    @property
    def result_feedback(self) -> ToolFeedback | None:
        for item in self.feedback:
            if item.kind is FeedbackKind.RESULT:
                return item
        return None


class ToolModule:
    """工具使用模块：收请求 → 引擎执行 → 返回反馈流与终态结果。"""

    def __init__(self, engine: ToolEngine) -> None:
        self.engine = engine

    @property
    def engine_name(self) -> str:
        return getattr(self.engine, "name", "unknown")

    def list_templates(self) -> tuple[str, ...]:
        return self.engine.list_templates()

    def submit(self, request: ToolRequest) -> ToolUseOutcome:
        feedback = tuple(self.iter_feedback(request))
        terminal = next(
            (
                item.result
                for item in reversed(feedback)
                if item.kind is FeedbackKind.RESULT and item.result is not None
            ),
            None,
        )
        result = terminal or ToolResult(
            status=ToolStatus.FAILED, error="no terminal result feedback"
        )
        return ToolUseOutcome(
            request_id=request.request_id,
            feedback=feedback,
            result=result,
        )

    def iter_feedback(self, request: ToolRequest, cancel=None):
        engine = self.engine
        iterator = getattr(engine, "iter_execute", None)
        if callable(iterator):
            if cancel is None:
                yield from iterator(request)
                return
            try:
                yield from iterator(request, cancel=cancel)
            except TypeError:
                yield from iterator(request)
            return
        yield from engine.execute(request)


class ToolRunner:
    """后台执行：``start`` 立刻返回；反馈逐条写入 ``HangStore``。

    ``on_feedback(task_id, item)`` 由 200 接包装队列。未注入时用规则占位写 ``visible``。
    ``cancel(task_id)`` 置位该次会话，引擎应停子进程。失败写成 ``result/failed``。
    """

    def __init__(
        self,
        module: ToolModule,
        store: HangStore,
        *,
        on_feedback: Callable[[str, ToolFeedback], None] | None = None,
    ) -> None:
        self.module = module
        self.store = store
        self.on_feedback = on_feedback
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._cancels: dict[str, threading.Event] = {}

    def start(self, request: ToolRequest, hang_id: str) -> None:
        cancel = threading.Event()
        thread = threading.Thread(
            target=self._run,
            args=(request, hang_id, cancel),
            name=f"tool-hang-{hang_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._threads.append(thread)
            self._cancels[hang_id] = cancel
        thread.start()

    def cancel(self, hang_id: str) -> None:
        with self._lock:
            flag = self._cancels.get(hang_id)
        if flag is not None:
            flag.set()

    def _apply_item(self, hang_id: str, item: ToolFeedback) -> None:
        self.store.append_feedback(hang_id, (item,))
        hook = self.on_feedback
        if hook is not None:
            hook(hang_id, item)
            return
        from .hang import rule_wrap_from_item

        wrapped = rule_wrap_from_item(item)
        if wrapped is None:
            return
        visible, summary = wrapped
        self.store.set_wrap(
            hang_id, visible=visible, summary=summary, terminal=True
        )

    def _run(
        self, request: ToolRequest, hang_id: str, cancel: threading.Event
    ) -> None:
        wrote_result = False
        try:
            for item in self.module.iter_feedback(request, cancel=cancel):
                self._apply_item(hang_id, item)
                if item.kind is FeedbackKind.RESULT:
                    wrote_result = True
            if cancel.is_set() and not wrote_result:
                self._apply_item(
                    hang_id,
                    ToolFeedback(
                        request_id=request.request_id,
                        kind=FeedbackKind.RESULT,
                        result=ToolResult(status=ToolStatus.ABORTED, error="已取消"),
                    ),
                )
        except Exception as exc:
            logger.exception("tool runner failed; writing failed result")
            failed = ToolFeedback(
                request_id=request.request_id,
                kind=FeedbackKind.RESULT,
                result=ToolResult(status=ToolStatus.FAILED, error=str(exc)),
            )
            self._apply_item(hang_id, failed)
        finally:
            with self._lock:
                self._cancels.pop(hang_id, None)

    def drain_for_tests(self, timeout: float = 5.0) -> None:
        with self._lock:
            threads = list(self._threads)
            self._threads.clear()
        for thread in threads:
            thread.join(timeout)

