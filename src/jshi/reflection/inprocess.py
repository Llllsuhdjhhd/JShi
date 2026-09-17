"""300 引擎（进程内）：探测器 → 判定气口 → 入队 → 现场 → 六问 → 落记录。

一次触发 = 一条自己的 04 ``kind=internal`` 活动，彼此不等待，也不等主流程（设计 §3.4）。

- ``enqueue_from_sources``：只入队，**不跑模型**（主流程钩子只调它）；
- ``drain``：同步执行口，由宿主在**两拍之间**调用（CLI 每轮之后 / ``introspect-idle`` / 测试）；
  常驻 daemon worker 见《方案 300》§5.10；
- ``reflect``：人工同步入口（CLI / 旧测试），直接跑完并返回 ``CognitiveContent``；
- ``assess``：**判定层气口**（投入 × 及时性由个人世界 + 心智决定，机制待完善，设计 §11-C15）；
  为 ``None`` 时用触发源默认值。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable, Sequence

from jshi.core.contracts import Provenance, SubjectState
from jshi.core.params import (
    INTROSPECTION_ENABLED,
    INTROSPECTION_RECALL_LIMIT,
    INTROSPECTION_SCENE_MAX_SEGMENTS,
    INTROSPECTION_SCENE_WINDOW_SECONDS,
)
from jshi.models import ModelPort, ModelRequest

from .port import (
    MOMENT_IDLE,
    NOT_EVALUATED,
    NO_MATERIAL,
    IntrospectionAnswer,
    IntrospectionLevel,
    IntrospectionPort,
    IntrospectionRequest,
    IntrospectionRun,
    IntrospectionScene,
    IntrospectionStatus,
    IntrospectionTrigger,
    IntrospectionUrgency,
    SourceContext,
    level_rank,
    utc_now,
)
from .queue import IntrospectionQueue
from .runner import run_introspection
from .scene import build_scene, render_scene
from .sources import default_sources, detect

logger = logging.getLogger(__name__)

AssessFn = Callable[[IntrospectionRequest], IntrospectionRequest]


class InProcessReflection(IntrospectionPort):
    """300 的最小完整实现（本刀：三个代表性源 + 六问 + 候选占位）。"""

    name = "in-process-reflection"

    def __init__(
        self,
        process: object,
        *,
        model: ModelPort | None = None,
        skill: object | None = None,
        sources: Sequence[object] | None = None,
        enabled: bool | None = None,
        queue: IntrospectionQueue | None = None,
        subject_state_factory: Callable[[str], SubjectState] | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._process = process
        self.repository = process.repository
        self.ledger = process.activity_ledger
        self.memory = getattr(process, "memory", None)
        self.hang_store = getattr(process, "hang_store", None)
        self._activity_close = process.activity_close
        self._enabled = INTROSPECTION_ENABLED if enabled is None else bool(enabled)
        self._now = now
        self._model = model if model is not None else getattr(process, "cognition", None)
        self._skill = skill
        if self._skill is None and self._model is not None:
            # 懒导入：避免 reflection ↔ jshi.skill 的模块级环
            from jshi.skill import IntrospectionSkill

            self._skill = IntrospectionSkill(self._model)
        self._sources: tuple[object, ...] = (
            tuple(sources) if sources is not None else default_sources()
        )
        self.queue = queue or IntrospectionQueue(now=now)
        self._subject_state_factory = subject_state_factory
        # 判定层气口：为 None 时用触发源默认值（设计 §11-C15）
        self.assess: AssessFn | None = None

    # ------------------------------------------------------------------
    # 入队（主流程钩子只走这里，不跑模型）
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def detect(
        self,
        moment: str,
        subject_id: str,
        *,
        activity: object | None = None,
        plan: object | None = None,
        action_id: str = "",
        object_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[IntrospectionRequest, ...]:
        context = SourceContext(
            subject_id=subject_id,
            moment=moment,
            now=now or self._now(),
            repository=self.repository,
            ledger=self.ledger,
            memory=self.memory,
            hang_store=self.hang_store,
            activity=activity,
            plan=plan,
            action_id=action_id,
            object_id=object_id,
        )
        try:
            return detect(tuple(self._sources), moment, context)
        except Exception:
            logger.exception("introspection detect failed")
            return ()

    def enqueue(self, request: IntrospectionRequest) -> IntrospectionStatus:
        if not self._enabled:
            self._record_dropped(request, "disabled")
            return IntrospectionStatus.DROPPED
        request = self._assess(request)
        result = self.queue.enqueue(request)
        if result.status is IntrospectionStatus.QUEUED:
            self._record_queued(request)
            if result.evicted is not None:
                self._record_dropped(result.evicted, "queue_full")
        else:
            self._record_dropped(request, result.reason or "dropped")
        return result.status

    def enqueue_from_sources(
        self,
        moment: str,
        subject_id: str,
        **context: Any,
    ) -> tuple[IntrospectionRequest, ...]:
        accepted: list[IntrospectionRequest] = []
        for request in self.detect(moment, subject_id, **context):
            if self.enqueue(request) is IntrospectionStatus.QUEUED:
                accepted.append(request)
        return tuple(accepted)

    def enqueue_idle(
        self, subject_id: str, *, object_id: str | None = None
    ) -> IntrospectionRequest | None:
        accepted = self.enqueue_from_sources(
            MOMENT_IDLE, subject_id, object_id=object_id
        )
        return accepted[0] if accepted else None

    # ------------------------------------------------------------------
    # 执行（宿主在两拍之间调）
    # ------------------------------------------------------------------

    def drain(
        self, subject_id: str | None = None, *, limit: int = 1
    ) -> tuple[IntrospectionRun, ...]:
        runs: list[IntrospectionRun] = []
        for _ in range(max(1, int(limit))):
            request = self.queue.pop_next(subject_id)
            if request is None:
                break
            runs.append(self._run(request))
        return tuple(runs)

    def reflect(self, subject_id: str, prompt: str) -> Any:
        """人工触发：同步跑完，返回 ``CognitiveContent``（CLI / 旧测试入口）。"""
        text = (prompt or "").strip()
        request = IntrospectionRequest(
            subject_id=subject_id,
            trigger=IntrospectionTrigger.MANUAL,
            entry_ref=f"manual:{text[:40] or 'introspection'}",
            entry_at=self._now(),
            entry_text=text,
            level=IntrospectionLevel.HIGH,
            urgency=IntrospectionUrgency.URGENT,
            origin="cli",
        )
        self._record_queued(request)
        run = self._run(request)
        if not run.cognitive_content_id:
            raise RuntimeError(f"introspection produced no content: {run.reason}")
        return self.repository.get_cognitive_content(run.cognitive_content_id)

    # ------------------------------------------------------------------
    # 内部：一次自省
    # ------------------------------------------------------------------

    def _run(self, request: IntrospectionRequest) -> IntrospectionRun:
        scene = build_scene(
            request,
            ledger=self.ledger,
            memory=self.memory,
            window_seconds=INTROSPECTION_SCENE_WINDOW_SECONDS,
            max_segments=INTROSPECTION_SCENE_MAX_SEGMENTS,
            recall_limit=INTROSPECTION_RECALL_LIMIT,
        )
        if scene is None and not _allows_empty_material(request):
            self.queue.finish(request)
            self._record_dropped(request, "no_material")
            return IntrospectionRun(
                request=request,
                scene_source_ids=(),
                answer=IntrospectionAnswer(what=NO_MATERIAL, mode="rule"),
                activity_id="",
                cognitive_content_id="",
                status=IntrospectionStatus.DROPPED,
                reason="no_material",
            )
        if scene is None:
            scene = _empty_scene(request)

        answer = self._answer(request, scene)
        try:
            run = run_introspection(
                request=request,
                scene=scene,
                answer=answer,
                repository=self.repository,
                activity_close=self._activity_close,
                model_tag=self._model_tag(answer),
            )
        except Exception:
            logger.exception("introspection run failed")
            self.queue.finish(request)
            return IntrospectionRun(
                request=request,
                scene_source_ids=scene.source_ids,
                answer=answer,
                activity_id="",
                cognitive_content_id="",
                status=IntrospectionStatus.FAILED,
                reason="run_failed",
            )
        self.queue.finish(request)
        return run

    def _answer(
        self, request: IntrospectionRequest, scene: IntrospectionScene
    ) -> IntrospectionAnswer:
        if self._skill is None or level_rank(request.level) <= level_rank(
            IntrospectionLevel.LOW
        ):
            return _rule_answer(request, scene)
        try:
            return self._skill.run(self._model_request(request, scene))
        except Exception:
            logger.exception("introspection skill failed")
            return IntrospectionAnswer(
                what="（自省失败：模型调用出错）",
                degraded=True,
                mode="fallback",
            )

    def _model_request(
        self, request: IntrospectionRequest, scene: IntrospectionScene
    ) -> ModelRequest:
        return ModelRequest(
            purpose="introspection",
            input_text=scene.text,
            subject_state=self._subject_state(request.subject_id),
        )

    def _subject_state(self, subject_id: str) -> SubjectState:
        if self._subject_state_factory is not None:
            return self._subject_state_factory(subject_id)
        return SubjectState(
            subject_id=subject_id,
            identity_summary="",
            current_stance="",
            provenance=Provenance(source="introspection"),
        )

    def _assess(self, request: IntrospectionRequest) -> IntrospectionRequest:
        if self.assess is None:
            return request
        try:
            assessed = self.assess(request)
        except Exception:
            logger.exception("introspection assess failed")
            return request
        return assessed or request

    def _model_tag(self, answer: IntrospectionAnswer) -> str:
        if answer.mode != "model" or self._skill is None:
            return ""
        return str(getattr(self._skill, "model_tag", "") or "")

    # ------------------------------------------------------------------
    # 内部：历史记录
    # ------------------------------------------------------------------

    def _record_queued(self, request: IntrospectionRequest) -> None:
        self._record("introspection_queued", request, extra=None)

    def _record_dropped(self, request: IntrospectionRequest, reason: str) -> None:
        self._record("introspection_dropped", request, extra={"reason": reason})

    def _record(
        self,
        event_type: str,
        request: IntrospectionRequest,
        *,
        extra: dict[str, Any] | None,
    ) -> None:
        # 延迟导入：避免 reflection → jshi.subject.__init__ → process → reflection 的环
        from jshi.subject.domain import HistoryKind, HistoryRecord

        content: dict[str, Any] = {
            "request_id": request.request_id,
            "trigger": request.trigger.value,
            "entry_ref": request.entry_ref,
            "entry_at": request.entry_at.isoformat(),
            "level": request.level.value,
            "urgency": request.urgency.value,
            "object_id": request.object_id,
            "origin": request.origin,
            "evidence": _json_safe(dict(request.evidence)),
        }
        if extra:
            content.update(extra)
        self.repository.add_history(
            HistoryRecord(
                subject_id=request.subject_id,
                kind=HistoryKind.SUBJECT,
                event_type=event_type,
                content=content,
                source_ids=(request.request_id,),
            )
        )


def _allows_empty_material(request: IntrospectionRequest) -> bool:
    """材料不足时还跑不跑：人工/急件跑（降级），其余不做（设计 §4.1：不硬编）。"""
    return request.trigger is IntrospectionTrigger.MANUAL or (
        request.urgency is IntrospectionUrgency.URGENT
    )


def _empty_scene(request: IntrospectionRequest) -> IntrospectionScene:
    window = (request.entry_at, request.entry_at)
    return IntrospectionScene(
        request=request,
        window=window,
        text=render_scene(request, (), (), window) + "\n（材料不足：本条没有可用经历与召回）",
    )


def _rule_answer(
    request: IntrospectionRequest, scene: IntrospectionScene
) -> IntrospectionAnswer:
    """低档（不调模型）：步骤不减、深度可变 —— ①② 给规则摘要，其余标未评。"""
    summary = (
        f"条目 {request.entry_ref}：{request.entry_text or '（无条目文本）'}；"
        f"现场 {len(scene.segments)} 段、回溯 {len(scene.recalled)} 条。"
    )
    return IntrospectionAnswer(
        what=summary,
        choice_and_result=NOT_EVALUATED,
        why=NOT_EVALUATED,
        better=NOT_EVALUATED,
        next_time=NOT_EVALUATED,
        mode="rule",
        degraded=False,
    )


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)
