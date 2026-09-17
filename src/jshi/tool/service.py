"""200 对外口：intake / list_visible / cancel。

主流程与 03 只调这三口。策划在后台；发出 ToolRequest 之后才建记挂。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contract import (
    AskMode,
    FeedbackKind,
    ToolEstimate,
    ToolFeedback,
    ToolOrigin,
    ToolRequest,
    ToolStatus,
    utc_now,
)
from .hang import (
    HangStore,
    OPEN_LIST_CAP,
    is_stage_fact,
    rule_wrap_from_item,
    truncate_note,
)
from .intake import IntakeRecord, IntakeStore
from .metrics import ToolMetricsStore
from .plan import PlanFailure, RulePlanner, ToolPlan, ToolPlanner, normalize_plan
from .port import ToolRunner
from .stage import (
    PHASE_CANCEL,
    PHASE_ESTIMATE,
    PHASE_PLANNING,
    PHASE_PROGRESS,
    PHASE_RESULT,
    PHASE_WRAP,
    stage_event,
)

logger = logging.getLogger(__name__)

PLACEHOLDER_DOWNSIDE = "占位估价；内容由引擎执行"

# 执行超时分档：用现成短、造工具长；估价可上调，有上下限。
TIMEOUT_USE_S = 120.0
TIMEOUT_CREATE_S = 600.0
TIMEOUT_MIN_S = 60.0
TIMEOUT_MAX_S = 900.0
TIMEOUT_ESTIMATE_FACTOR = 3.0


def resolve_timeout_s(kind: str, estimate: Mapping[str, Any] | None = None) -> float:
    """按动作分档，并可被 time_est_ms 放大（不超过封顶）。"""
    base = TIMEOUT_CREATE_S if (kind or "") == "create" else TIMEOUT_USE_S
    est = estimate if isinstance(estimate, Mapping) else {}
    raw = est.get("time_est_ms")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        base = max(base, float(raw) / 1000.0 * TIMEOUT_ESTIMATE_FACTOR)
    return max(TIMEOUT_MIN_S, min(TIMEOUT_MAX_S, base))


# 片场工具块：单条上限与同场条数上限（魔法数，待调）。
SCENE_BLOCK_CHARS = 240
SCENE_BLOCK_CAP = 3
# 旧版片场工具块前缀（已废止写入；仅用于统计历史块字数）。
SCENE_BLOCK_PREFIX = "（我知道）"

# 近时窗口：进行中一律保留；终态（完成/失败）保留一段时间给 05 判断要不要再开。
# 时长由程序配置，不在提示词里写死。
HOT_STATE_TTL_S = 3600
# 策划中（intake received、尚无记挂）：超过此时长视为卡死，自动标失败。
PLANNING_TTL_S = 300
HOT_STATE_CAP = 12
_HOT_FAILED_STATUSES = frozenset(
    {
        ToolStatus.FAILED,
        ToolStatus.PARTIAL,
        ToolStatus.ABORTED,
        ToolStatus.REJECTED,
        ToolStatus.BUDGET_EXCEEDED,
    }
)
_HOT_PHASE_LABEL = {
    "running": "进行中",
    "done": "已完成",
    "failed": "已失败",
}
_RELATED_STATUS = {
    "running": "使用中",
    "done": "已完成",
    "failed": "失败",
}
_DEDUP_PLAN_MARKERS = (
    "已经在办",
    "不再另开",
    "同一件事",
    "还在办",
    "还在跑",
)


def _normalize_tool_related_key(tool_name: str) -> str:
    """同一工具归并键：去掉 skill: 前缀，统一连字符。"""
    key = (tool_name or "").strip().lower()
    if key.startswith("skill:"):
        key = key[6:]
    key = key.replace("_", "-")
    # 提案未执行与已执行同命令仍算同一工具名主干
    for suffix in ("（尚未执行）", "(尚未执行)"):
        if key.endswith(suffix):
            key = key[: -len(suffix)].strip()
    return key or "工具"


def _entry_used_at(item: Mapping[str, Any]) -> datetime:
    at = item.get("used_at")
    if isinstance(at, datetime):
        if at.tzinfo is None:
            return at.replace(tzinfo=timezone.utc)
        return at
    return datetime.min.replace(tzinfo=timezone.utc)


def dedupe_recent_tool_entries(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """近期完成：同一工具只留最后一次成功与最后一次失败；使用中原样保留。"""
    in_use: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    for raw in entries:
        item = dict(raw)
        section = str(item.get("section") or "")
        if section == "in_use":
            in_use.append(item)
        elif section == "recent":
            recent.append(item)
    recent.sort(key=_entry_used_at, reverse=True)
    best_ok: dict[str, dict[str, Any]] = {}
    best_fail: dict[str, dict[str, Any]] = {}
    for item in recent:
        key = _normalize_tool_related_key(str(item.get("tool_name") or ""))
        status = str(item.get("status") or "").strip()
        if status == "已完成":
            if key not in best_ok:
                best_ok[key] = item
        elif status == "失败":
            if key not in best_fail:
                best_fail[key] = item
    kept = list(best_ok.values()) + list(best_fail.values())
    kept.sort(key=_entry_used_at, reverse=True)
    return in_use + kept


@dataclass(frozen=True)
class ToolHotItem:
    """给 05 看的工具热条目：进行中，或结束未满 TTL。"""

    id: str
    phase: str  # running | done | failed
    source: str  # hang | plan_failed
    command: str = ""
    need: str = ""
    summary: str = ""
    updated_at: datetime = field(default_factory=utc_now)


def hang_hot_phase(hang) -> str:
    """记挂热相位：open→running；notified 看末条 RESULT。"""
    if getattr(hang, "status", "") == "open":
        return "running"
    for item in reversed(tuple(getattr(hang, "feedback", ()) or ())):
        if getattr(item, "kind", None) is not FeedbackKind.RESULT:
            continue
        result = getattr(item, "result", None)
        if result is None:
            continue
        status = getattr(result, "status", None)
        if status in _HOT_FAILED_STATUSES:
            return "failed"
        return "done"
    return "done" if (getattr(hang, "summary", "") or "").strip() else "failed"


def format_tool_hot_state(
    items: tuple[ToolHotItem, ...] | list[ToolHotItem],
    *,
    now: datetime | None = None,
) -> str:
    """旧热状态块（兼容测试/调试）。主路径改用 ``format_tool_related``。"""
    if not items:
        return ""
    from jshi.models.prompt import relative_time_label

    lines = ["【工具热状态】"]
    for item in items:
        label = _HOT_PHASE_LABEL.get(item.phase, item.phase)
        bits = [label]
        if item.command:
            bits.append(item.command)
        elif item.source == "plan_failed":
            bits.append("策划")
        if (item.need or "").strip():
            bits.append(item.need.strip())
        summary = (item.summary or "").strip()
        if summary:
            bits.append(summary[:120])
        when = relative_time_label(item.updated_at, now=now)
        if when:
            bits.append(when)
        lines.append("- " + " · ".join(bits))
    return "\n".join(lines)


def format_tool_related(
    entries: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> str:
    """渲染给 05 的【工具相关】块；两节皆空则整段不出现。

    每条 ``entries`` 至少含：``section``（in_use|recent）、``status``、
    ``need``、``tool_name``、``tool_description``、``used_at``；
    使用中可有 ``new_info``；近期完成可有 ``final_result``。
    """
    in_use = [item for item in entries if str(item.get("section") or "") == "in_use"]
    recent = [item for item in entries if str(item.get("section") or "") == "recent"]
    if not in_use and not recent:
        return ""
    from jshi.models.prompt import relative_time_label

    lines = [
        "【工具相关】",
        "读法：绑定当前对话对象的工具材料，不是对方原话，也不是回忆。"
        "用来答未了结的事、消化刚到的结果、判断要不要再开工具。",
    ]

    def _emit(item: Mapping[str, Any]) -> None:
        need = str(item.get("need") or "").strip() or "（无需求原文）"
        name = str(item.get("tool_name") or "").strip() or "（未命名）"
        desc = str(item.get("tool_description") or "").strip() or name
        status = str(item.get("status") or "").strip()
        used_at = item.get("used_at")
        when = ""
        if isinstance(used_at, datetime):
            when = relative_time_label(used_at, now=now) or ""
        lines.append(f"- 需求：{need}")
        lines.append(f"  工具名称：{name}")
        if when:
            lines.append(f"  使用时间：{when}")
        lines.append(f"  工具描述：{desc}")
        new_info = str(item.get("new_info") or "").strip()
        if new_info:
            lines.append(f"  新的信息：{new_info}")
        final_result = str(item.get("final_result") or "").strip()
        if final_result:
            lines.append(f"  最终的结果：{final_result}")
        if status:
            lines.append(f"  状态：{status}")

    if in_use:
        lines.append("")
        lines.append("# 使用中的工具")
        for item in in_use:
            _emit(item)
    if recent:
        lines.append("")
        lines.append("# 近期使用完成的工具")
        for item in recent:
            _emit(item)
    return "\n".join(lines)

# 解锁依赖的终态：只有真正做成。PARTIAL 是「部分工具未完成」，喂给依赖它的
# 下一步等于拿不确定当确定；冻结并把原因记在计划状态里更安全。
PLAN_SATISFIED_STATUSES = frozenset({ToolStatus.OK})

# 计划的运行态。计划本身只在内存里（见《计划落账与调度语义》§3）。
PLAN_RUNNING = "running"
PLAN_BLOCKED = "blocked"
PLAN_CANCELLED = "cancelled"
PLAN_DONE = "done"
PLAN_FROZEN = frozenset({PLAN_BLOCKED, PLAN_CANCELLED})


@dataclass(frozen=True)
class VisibleToolItem:
    """03 可见条目。``summary`` 是交给 03 的正文，组装原样装入，不改写。"""

    id: str
    summary: str
    subject_id: str
    object_id: str
    kind: str  # plan_failed | hang
    updated_at: datetime = field(default_factory=utc_now)


WRAP_CONCURRENCY = 2


def format_scene_block(summary: str) -> str:
    """200 修饰：把可见人话写成片场工具块的正文。

    形态是「匠石当前知道的事实」，不是「我说」。块号由片场渲染重排，此处不带编号。
    """
    text = (summary or "").strip()
    if not text:
        return ""
    body = text if text.startswith(SCENE_BLOCK_PREFIX) else SCENE_BLOCK_PREFIX + text
    if len(body) > SCENE_BLOCK_CHARS:
        body = body[:SCENE_BLOCK_CHARS]
    return body


def _proposal_summary(
    request: ToolRequest, estimate: Mapping[str, Any]
) -> str:
    benefit = str(estimate.get("benefit") or "").strip() or truncate_note(
        request.need
    )
    downside = str(estimate.get("downside") or "").strip()
    need_confirm = bool(estimate.get("need_confirm", False))
    parts = [f"提议：{benefit}"]
    if downside:
        parts.append(f"风险：{downside}")
    if need_confirm:
        parts.append("需要确认")
    return "；".join(parts)


def _hang_launch_meta(plan_tag: str, request: ToolRequest) -> dict[str, str]:
    """记挂 meta：策划标签 + 造工具时的能力说明（供 205 in_flight 的 purpose）。"""
    meta: dict[str, str] = {}
    if plan_tag:
        meta["plan_model_tag"] = plan_tag
    create = request.create
    if create is not None:
        intent = (create.tool_intent or "").strip()
        if intent:
            meta["tool_intent"] = intent
        expected = (create.expected_output or "").strip()
        if expected:
            meta["expected_output"] = expected
    return meta


def _command_from_result(result: Any) -> str:
    execution = getattr(result, "execution", None) or ()
    if execution:
        tool = execution[0].get("tool")
        if tool:
            return str(tool).strip()
    raw = getattr(result, "result", None)
    if isinstance(raw, Mapping):
        tool = raw.get("tool")
        if tool:
            return str(tool).strip()
    return ""


def _result_tokens_for_stage(result: Any) -> float | None:
    resource = getattr(result, "resource", None) or {}
    for key in ("totalTokens", "input", "output"):
        value = resource.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _plan_to_dict(plan: ToolPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "need": plan.need,
        "mode": plan.mode,
        "steps": [
            {
                "step_id": step.step_id,
                "command": step.request.command,
                "params": dict(step.request.params),
                "expected_result": step.request.expected_result,
                "ask": step.request.ask.value,
                "depends_on": list(step.depends_on),
                "parallel_group": step.parallel_group,
            }
            for step in plan.steps
        ],
    }


def _tool_json_for_path(path: str) -> Path | None:
    if not path:
        return None
    base = Path(path)
    if base.is_file():
        return base.with_name("tool.json")
    return base / "tool.json"


def _is_cancelled(record: Any) -> bool:
    return getattr(record, "status", "") == "cancelled"


class ToolService:
    """200 门面。可重入：每次 intake 各开后台，不拿一把全局锁堵住第二次。"""

    def __init__(
        self,
        hang_store: HangStore,
        runner: ToolRunner,
        *,
        intake_store: IntakeStore | None = None,
        planner: ToolPlanner | None = None,
        wrap_skill: Any | None = None,
        intake_path: str | Path | None = None,
        wrap_concurrency: int = WRAP_CONCURRENCY,
    ) -> None:
        self.hang_store = hang_store
        self.runner = runner
        if intake_store is not None:
            self.intake_store = intake_store
        else:
            path = Path(intake_path) if intake_path is not None else hang_store.path.parent / "tool.jsonl"
            self.intake_store = IntakeStore(path)
        self.planner = planner or RulePlanner()
        if hasattr(self.planner, "hang_store") and getattr(
            self.planner, "hang_store", None
        ) is None:
            self.planner.hang_store = hang_store
        self.wrap_skill = wrap_skill
        self._lock = threading.Lock()
        self._dedupe_lock = threading.Lock()
        self._planner_threads: list[threading.Thread] = []
        self._wrap_slots = threading.Semaphore(max(1, wrap_concurrency))
        self._wrap_lock = threading.Lock()
        self._wrap_pending: dict[str, list] = {}
        self._wrap_workers: dict[str, threading.Thread] = {}
        self._wrap_threads: list[threading.Thread] = []
        self.metrics = ToolMetricsStore(hang_store.path.parent / "tool_metrics.json")
        self._engine_tool_paths: dict[str, str] = {}
        # 这一次是「造」的记挂。造成功了要让 205 重读引擎目录，否则新工具检索不到。
        self._create_tasks: set[str] = set()
        # propose_only 的待批准请求。只在当前进程内可批准；重启后为空，
        # 结果是“不执行”，而不是误执行。
        self._proposed_requests: dict[str, ToolRequest] = {}
        # 多工具计划：仅当前进程内调度，重启后按未完成处理，不误执行。
        self._plans: dict[str, ToolPlan] = {}
        self._plan_lock = threading.Lock()
        self._plan_intake_ids: dict[str, str] = {}
        self._plan_task_to_plan: dict[str, str] = {}
        self._plan_task_to_step: dict[str, str] = {}
        self._plan_step_tasks: dict[str, dict[str, str]] = {}
        self._plan_agent_tasks: dict[str, str] = {}
        self._plan_completed: dict[str, set[str]] = {}
        self._plan_status: dict[str, str] = {}
        self._plan_note: dict[str, str] = {}
        self.runner.on_feedback = self._on_engine_feedback

    def intake(
        self,
        *,
        subject_id: str,
        object_id: str,
        activity_id: str = "",
        need: str = "",
        verbal: str = "",
        field_ref: Mapping[str, str] | None = None,
        origin: str = "external_05",
    ) -> IntakeRecord:
        # 新开前先清僵尸 open / 超时策划，避免 205 一直以为「还在办」。
        if (subject_id or "").strip() and (object_id or "").strip():
            self.reap_stale(subject_id, object_id)
        need_key = (need or "").strip()
        with self._dedupe_lock:
            if need_key:
                existing = self.hang_store.find_open_by_need(
                    subject_id, object_id, need_key
                )
                if existing is not None:
                    record = self.intake_store.create(
                        subject_id=subject_id,
                        object_id=object_id,
                        activity_id=activity_id,
                        need=need,
                        verbal=verbal,
                        field_ref=field_ref,
                        origin=origin,
                    )
                    self.intake_store.update(
                        record.intake_id,
                        status="launched",
                        request_id=existing.request_id,
                        task_id=existing.task_id,
                        meta={
                            "dedupe": "need",
                            "dedupe_task_id": existing.task_id,
                        },
                    )
                    return self.intake_store.get(record.intake_id) or record
            record = self.intake_store.create(
                subject_id=subject_id,
                object_id=object_id,
                activity_id=activity_id,
                need=need,
                verbal=verbal,
                field_ref=field_ref,
                origin=origin,
            )
        thread = threading.Thread(
            target=self._plan_and_launch,
            args=(record.intake_id,),
            name=f"tool-plan-{record.intake_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._planner_threads.append(thread)
        thread.start()
        return record

    def _plan_and_launch(self, intake_id: str) -> None:
        record = self.intake_store.get(intake_id)
        if record is None or record.status == "cancelled":
            return
        tag = self._planner_tag()
        try:
            planned = self.planner.plan(record)
        except Exception:
            logger.exception("tool planner failed")
            self.intake_store.update(
                intake_id,
                status="failed",
                plan_error="策划出错",
                meta={"model_tag": tag} if tag else {},
            )
            return
        current = self.intake_store.get(intake_id)
        if current is None or current.status == "cancelled":
            return
        if isinstance(planned, PlanFailure):
            self._fail_intake(intake_id, planned.message, tag)
            return
        if isinstance(planned, ToolPlan):
            normalized = normalize_plan(planned)
            if isinstance(normalized, PlanFailure):
                self._fail_intake(intake_id, normalized.message, tag)
                return
            self._launch_plan(current, normalized, plan_tag=tag)
        else:
            self._launch(current, planned, plan_tag=tag)

    def _fail_intake(self, intake_id: str, message: str, tag: str = "") -> None:
        """策划（或计划归一化）失败：交接转 failed，失败句走可见路径，不建记挂。"""
        self.intake_store.append_stage(
            intake_id,
            stage_event(
                intake_id,
                PHASE_PLANNING,
                status="failed",
                text=message,
                meta={"model_tag": tag},
            ),
        )
        self.intake_store.update(
            intake_id,
            status="failed",
            plan_error=message,
            meta={"model_tag": tag} if tag else {},
        )

    def _planner_tag(self) -> str:
        skill = getattr(self.planner, "skill", None)
        return str(getattr(skill, "model_tag", "") or "")

    def _finish_create(self, task_id: str, item: ToolFeedback) -> None:
        """造工具的记挂收到终态：造成功了就让 205 下次重读引擎目录。

        不重读的话，同一会话里新造出来的工具检索不到，于是会重复造。
        """
        if task_id not in self._create_tasks:
            return
        self._create_tasks.discard(task_id)
        result = item.result
        if result is None or result.status is not ToolStatus.OK:
            return
        invalidate = getattr(self.planner, "invalidate_tools", None)
        if callable(invalidate):
            invalidate()

    def _launch(
        self,
        intake: IntakeRecord,
        request: ToolRequest,
        *,
        plan_tag: str = "",
        plan_ctx: Mapping[str, Any] | None = None,
    ) -> str | None:
        ctx = dict(plan_ctx or {})
        is_create = request.create is not None or request.ask is AskMode.CREATE_TOOL
        kind = (
            "create"
            if is_create
            else "propose"
            if request.ask is AskMode.PROPOSE_ONLY
            else "use"
        )
        command = ""
        if is_create and request.create is not None:
            command = (request.create.tool_name or "created-tool").strip()
        elif request.command:
            command = (request.command or "").strip()
        plan_id = str(ctx.get("plan_id") or "")
        with self._dedupe_lock:
            if command:
                existing = self.hang_store.find_open_by_command(
                    intake.subject_id,
                    intake.object_id,
                    command,
                    exclude_plan_id=plan_id,
                )
                if existing is not None:
                    launched_meta = dict(intake.meta)
                    if plan_tag:
                        launched_meta["model_tag"] = plan_tag
                    launched_meta["dedupe"] = "command"
                    launched_meta["dedupe_task_id"] = existing.task_id
                    self.intake_store.update(
                        intake.intake_id,
                        status="launched",
                        request_id=existing.request_id,
                        task_id=existing.task_id,
                        meta=launched_meta,
                    )
                    return existing.task_id
            hang = self.hang_store.create(
                subject_id=intake.subject_id,
                object_id=intake.object_id,
                need=request.need or intake.need,
                template=request.template or "",
                command=command,
                kind=kind,
                field_ref=intake.field_ref,
                params=request.params,
                plan_id=plan_id,
                step_id=str(ctx.get("step_id") or ""),
                plan_mode=str(ctx.get("plan_mode") or ""),
                depends_on=tuple(ctx.get("depends_on") or ()),
                plan_index=int(ctx.get("plan_index") or 0),
                plan_total=int(ctx.get("plan_total") or 0),
                origin=intake.origin,
                request_id=request.request_id,
                meta=_hang_launch_meta(plan_tag, request),
            )
            # 必须在 runner.start 之前挂回计划：引擎可能在 start 之后立刻交出终态，
            # 那时 _plan_on_result 要能认出这个 task 属于哪个计划、哪一步。
            self._register_plan_task(hang.task_id, ctx)
        est_meta = request.meta.get("estimate") if isinstance(request.meta, Mapping) else {}
        if not isinstance(est_meta, Mapping):
            est_meta = {}
        timeout_s = resolve_timeout_s(kind, est_meta)
        bound = replace(
            request,
            request_id=hang.request_id,
            subject_id=intake.subject_id,
            activity_id=intake.activity_id or request.activity_id,
            field_ref=dict(intake.field_ref) or dict(request.field_ref),
            meta={
                **dict(request.meta),
                "timeout_s": timeout_s,
                "estimate": dict(est_meta),
            },
        )
        estimate = ToolFeedback(
            request_id=hang.request_id,
            kind=FeedbackKind.ESTIMATE,
            estimate=ToolEstimate(
                benefit=str(est_meta.get("benefit") or "").strip()
                or truncate_note(bound.need or intake.need),
                downside=str(est_meta.get("downside") or "").strip()
                or PLACEHOLDER_DOWNSIDE,
                time_est_ms=est_meta.get("time_est_ms"),
                cost_est=est_meta.get("cost_est"),
                need_confirm=bool(est_meta.get("need_confirm", False)),
            ),
        )
        self.hang_store.append_feedback(hang.task_id, (estimate,))
        self.hang_store.append_stage(
            hang.task_id,
            stage_event(
                hang.task_id,
                PHASE_PLANNING,
                kind=kind,
                status="ok",
                command=command,
                meta={"model_tag": plan_tag, "estimate": dict(est_meta)},
            ),
        )
        self.hang_store.append_stage(
            hang.task_id,
            stage_event(
                hang.task_id,
                PHASE_ESTIMATE,
                kind=kind,
                status="pending",
                text=estimate.estimate.benefit if estimate.estimate else "",
                metrics={
                    "time_est_ms": estimate.estimate.time_est_ms
                    if estimate.estimate
                    else None,
                    "cost_est": estimate.estimate.cost_est
                    if estimate.estimate
                    else None,
                },
            ),
        )
        launched_meta = dict(intake.meta)
        if plan_tag:
            launched_meta["model_tag"] = plan_tag
        if bound.ask is AskMode.PROPOSE_ONLY:
            self._proposed_requests[hang.task_id] = bound
            self.hang_store.set_wrap(
                hang.task_id,
                visible=True,
                summary=_proposal_summary(bound, est_meta),
                wrap_meta={"source": "proposal"},
                terminal=False,
            )
            self.intake_store.update(
                intake.intake_id,
                status="proposed",
                request_id=hang.request_id,
                task_id=hang.task_id,
                meta=launched_meta,
            )
            return hang.task_id
        self.intake_store.update(
            intake.intake_id,
            status="launched",
            request_id=hang.request_id,
            task_id=hang.task_id,
            meta=launched_meta,
        )
        if bound.create is not None:
            self._create_tasks.add(hang.task_id)
        self.runner.start(bound, hang.task_id)
        return hang.task_id

    def _register_plan_task(self, task_id: str, ctx: Mapping[str, Any]) -> None:
        """把任务挂回计划身份表。调用点必须在 runner.start 之前。"""
        plan_id = str(ctx.get("plan_id") or "")
        if not plan_id:
            return
        self._plan_task_to_plan[task_id] = plan_id
        if ctx.get("is_agent_loop"):
            self._plan_agent_tasks[plan_id] = task_id
            return
        step_id = str(ctx.get("step_id") or "")
        if step_id:
            self._plan_task_to_step[task_id] = step_id
            self._plan_step_tasks.setdefault(plan_id, {})[step_id] = task_id

    def _launch_plan(
        self, intake: IntakeRecord, plan: ToolPlan, *, plan_tag: str = ""
    ) -> None:
        """启动一个多工具计划。依赖满足时逐步启动后续步骤。"""
        self._plans[plan.plan_id] = plan
        self._plan_intake_ids[plan.plan_id] = intake.intake_id
        self._plan_status[plan.plan_id] = PLAN_RUNNING
        if plan.mode == "agent_loop":
            self._launch_agent_loop(intake, plan, plan_tag=plan_tag)
            return
        self._plan_step_tasks[plan.plan_id] = {}
        self._plan_completed[plan.plan_id] = set()
        self._launch_ready_steps(intake, plan, plan_tag=plan_tag)

    def _launch_agent_loop(
        self,
        intake: IntakeRecord,
        plan: ToolPlan,
        *,
        plan_tag: str = "",
    ) -> None:
        """把依赖型多工具计划交给 Pi 一个 agent loop。"""
        record = self.intake_store.create(
            subject_id=intake.subject_id,
            object_id=intake.object_id,
            activity_id=intake.activity_id,
            need=plan.need,
            verbal=intake.verbal,
            field_ref=intake.field_ref,
            origin=intake.origin,
            intake_id=f"{intake.intake_id}:agent",
        )
        request = ToolRequest(
            subject_id=intake.subject_id,
            activity_id=intake.activity_id,
            origin=ToolOrigin.EXTERNAL_05,
            need=plan.need,
            template="generic",
            command="",
            ask=AskMode.EXECUTE,
            meta={"agent_plan": _plan_to_dict(plan)},
        )
        task_id = self._launch(
            record,
            request,
            plan_tag=plan_tag,
            plan_ctx={
                "plan_id": plan.plan_id,
                "plan_mode": plan.mode,
                "plan_total": len(plan.steps),
                "is_agent_loop": True,
            },
        )
        if task_id:
            self._plan_agent_tasks.setdefault(plan.plan_id, task_id)

    def _launch_ready_steps(
        self,
        intake: IntakeRecord,
        plan: ToolPlan,
        *,
        plan_tag: str = "",
    ) -> None:
        with self._plan_lock:
            if self._plan_status.get(plan.plan_id) in PLAN_FROZEN:
                return  # 计划已冻结 / 已取消：不再启动任何新步骤
            completed = self._plan_completed.get(plan.plan_id, set())
            task_map = self._plan_step_tasks.setdefault(plan.plan_id, {})
            total = len(plan.steps)
            for index, step in enumerate(plan.steps, start=1):
                if step.step_id in task_map or step.step_id in completed:
                    continue
                if not set(step.depends_on).issubset(completed):
                    continue
                step_intake_id = f"{intake.intake_id}:{step.step_id}"
                step_record = self.intake_store.create(
                    subject_id=intake.subject_id,
                    object_id=intake.object_id,
                    activity_id=intake.activity_id,
                    need=step.request.need or plan.need,
                    verbal=intake.verbal,
                    field_ref=intake.field_ref,
                    origin=intake.origin,
                    intake_id=step_intake_id,
                )
                self._launch(
                    step_record,
                    step.request,
                    plan_tag=plan_tag,
                    plan_ctx={
                        "plan_id": plan.plan_id,
                        "step_id": step.step_id,
                        "plan_mode": plan.mode,
                        "depends_on": step.depends_on,
                        "plan_index": index,
                        "plan_total": total,
                    },
                )
                # task_map 已由 _register_plan_task 在 runner.start 之前填好

    def _plan_on_result(self, task_id: str, status: ToolStatus) -> None:
        """一个步骤收到终态：只有真正做成才解锁后续，其余一律冻结计划。"""
        plan_id = self._plan_task_to_plan.get(task_id)
        if not plan_id:
            return
        plan = self._plans.get(plan_id)
        if plan is None:
            return
        if self._plan_status.get(plan_id) in PLAN_FROZEN:
            return
        step_id = self._plan_task_to_step.get(task_id)
        if not step_id:
            # agent_loop：整本记挂就是整个计划。
            self._plan_status[plan_id] = (
                PLAN_DONE if status in PLAN_SATISFIED_STATUSES else PLAN_BLOCKED
            )
            self._plan_note[plan_id] = f"agent:{status.value}"
            return
        if status not in PLAN_SATISFIED_STATUSES:
            self._plan_status[plan_id] = PLAN_BLOCKED
            self._plan_note[plan_id] = f"{step_id}:{status.value}"
            logger.info(
                "tool plan %s blocked by step %s (%s)", plan_id, step_id, status.value
            )
            return
        self._plan_completed.setdefault(plan_id, set()).add(step_id)
        if len(self._plan_completed[plan_id]) >= len(plan.steps):
            self._plan_status[plan_id] = PLAN_DONE
        intake_id = self._plan_intake_ids.get(plan_id)
        if intake_id:
            intake = self.intake_store.get(intake_id)
            if intake is not None:
                self._launch_ready_steps(intake, plan)

    def _plan_intake(self, plan_id: str) -> IntakeRecord | None:
        for task_id, owner in self._plan_task_to_plan.items():
            if owner == plan_id:
                record = self.intake_store.get_by_task_id(task_id)
                if record is not None:
                    return record
        return None

    def approve(self, item_id: str) -> bool:
        """批准一个 propose_only 提案，改走 execute。只处理当前进程内的提案。"""
        request = self._proposed_requests.pop(item_id, None)
        if request is None:
            return False
        hang = self.hang_store.get(item_id)
        if hang is None or hang.status == "cancelled":
            return False
        approved = replace(request, ask=AskMode.EXECUTE)
        intake = self.intake_store.get_by_task_id(item_id)
        if intake is not None and intake.status == "proposed":
            self.intake_store.update(intake.intake_id, status="launched")
        self.runner.start(approved, item_id)
        return True

    def cancel_plan(self, plan_id: str) -> bool:
        """取消整个计划：先标终止，再取消已启动的步骤。

        标记必须在取消之前——否则某个步骤的 ABORTED 会先回到 `_plan_on_result`。
        已终止的再调一次仍返回 True（幂等），方便调用方当「已确保不再推进」用。
        """
        plan = self._plans.get(plan_id)
        if plan is None:
            return False
        self._plan_status[plan_id] = PLAN_CANCELLED
        self._plan_note[plan_id] = "cancelled"
        for task_id in list(self._plan_step_tasks.get(plan_id, {}).values()):
            self.cancel(task_id)
        agent_task = self._plan_agent_tasks.get(plan_id)
        if agent_task:
            self.cancel(agent_task)
        return True

    def plan_summary(self, plan_id: str) -> dict[str, Any] | None:
        """只读汇总一个计划当前各 step 的可见状态。"""
        plan = self._plans.get(plan_id)
        if plan is None:
            return None
        step_tasks = self._plan_step_tasks.get(plan_id, {})
        steps = []
        for step in plan.steps:
            task_id = step_tasks.get(step.step_id)
            hang = self.hang_store.get(task_id) if task_id else None
            steps.append(
                {
                    "step_id": step.step_id,
                    "task_id": task_id,
                    "status": hang.status if hang else "pending",
                    "summary": hang.summary if hang else "",
                }
            )
        agent_task = self._plan_agent_tasks.get(plan_id)
        agent_hang = self.hang_store.get(agent_task) if agent_task else None
        return {
            "plan_id": plan_id,
            "mode": plan.mode,
            "need": plan.need,
            "status": self._plan_status.get(plan_id, PLAN_RUNNING),
            "note": self._plan_note.get(plan_id, ""),
            "steps": steps,
            "agent_task_id": agent_task,
            "agent_status": agent_hang.status if agent_hang else "",
        }

    def _on_engine_feedback(self, task_id: str, item: ToolFeedback) -> None:
        if not is_stage_fact(item):
            return
        hang = self.hang_store.get(task_id)
        # 已注销的记挂不再推进任何状态机。Runner 侧已经拦了一层，
        # 这里是第二道防线——取消绝不能反过来解锁依赖它的后续步骤。
        if hang is None or hang.status == "cancelled":
            return
        if item.kind is FeedbackKind.RESULT:
            self._finish_create(task_id, item)
            self._record_tool_result(item, hang)
            self._plan_on_result(
                task_id,
                item.result.status if item.result is not None else ToolStatus.FAILED,
            )
            result = item.result
            self.hang_store.append_stage(
                task_id,
                stage_event(
                    task_id,
                    PHASE_RESULT,
                    kind=getattr(hang, "kind", "use") if hang else "use",
                    status=result.status.value if result else "failed",
                    text=(result.summary or result.error or "").strip()
                    if result
                    else "",
                    command=getattr(hang, "command", "") if hang else "",
                    metrics={
                        "time_ms": result.time_ms if result else None,
                        "cost": result.cost if result else None,
                        "tokens": _result_tokens_for_stage(result)
                        if result
                        else None,
                    },
                ),
            )
        elif item.kind is FeedbackKind.PROGRESS and item.progress is not None:
            self.hang_store.append_stage(
                task_id,
                stage_event(
                    task_id,
                    PHASE_PROGRESS,
                    kind=getattr(hang, "kind", "use") if hang else "use",
                    status="running",
                    text=item.progress.partial or "",
                    stage_name=item.progress.stage or "",
                    command=getattr(hang, "command", "") if hang else "",
                    metrics={
                        "time_used_ms": item.progress.time_used_ms,
                        "cost_used": item.progress.cost_used,
                    },
                ),
            )
        if self.wrap_skill is None:
            wrapped = rule_wrap_from_item(item)
            if wrapped is None:
                return
            visible, summary = wrapped
            self.hang_store.set_wrap(
                task_id,
                visible=visible,
                summary=summary,
                terminal=item.kind is FeedbackKind.RESULT,
            )
            return
        with self._wrap_lock:
            self._wrap_pending.setdefault(task_id, []).append(item)
            worker = self._wrap_workers.get(task_id)
            if worker is not None and worker.is_alive():
                return
            thread = threading.Thread(
                target=self._wrap_loop,
                args=(task_id,),
                name=f"tool-wrap-{task_id[:8]}",
                daemon=True,
            )
            self._wrap_workers[task_id] = thread
            self._wrap_threads.append(thread)
            thread.start()

    def _record_tool_result(
        self, item: ToolFeedback, hang: Any | None
    ) -> None:
        """把终态里的时间 / token / 费用累计到 command 级统计。

        只统计真正「使用工具」的记挂；创建 / 报价过程仍进记挂，但不污染
        该工具后续的实测均值。
        """
        if item.result is None:
            return
        if item.result.status in {ToolStatus.ABORTED, ToolStatus.REJECTED}:
            # 取消 / 从未发出：不算这个工具跑了一次，别污染实测均值与 tool.json。
            return
        if hang is None or getattr(hang, "kind", "use") != "use":
            return
        command = (getattr(hang, "command", "") or "").strip()
        if not command:
            command = _command_from_result(item.result)
        if not command:
            return
        self.metrics.record(command, item.result)
        self._refresh_tool_meta(command)

    def _refresh_tool_meta(self, command: str) -> None:
        """把实测均值写回 Pi skill 的 ``tool.json``，让下一轮 205 读到。"""
        engine = getattr(self.runner.module, "engine", None)
        if engine is None or getattr(engine, "name", "") != "pi":
            return
        path = self._engine_tool_paths.get(command)
        if path is None and not self._engine_tool_paths:
            self._load_engine_tool_paths(engine)
            path = self._engine_tool_paths.get(command)
        if not path:
            return
        stats = self.metrics.stats(command)
        sidecar = _tool_json_for_path(path)
        if sidecar is None:
            return
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data["observed"] = {
            "runs": stats.runs,
            "avg_time_ms": stats.avg_time_ms,
            "avg_cost": stats.avg_cost,
            "avg_tokens": stats.avg_tokens,
        }
        try:
            sidecar.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.warning("写 tool.json 实测统计失败：%s", sidecar)
        invalidate = getattr(self.planner, "invalidate_tools", None)
        if callable(invalidate):
            invalidate()

    def _load_engine_tool_paths(self, engine: Any) -> None:
        try:
            for item in engine.list_commands():
                name = str(item.get("name") or "").strip()
                path = str(item.get("path") or "").strip()
                if name and path:
                    self._engine_tool_paths[name] = path
        except Exception:
            logger.exception("读取引擎工具路径失败")

    def _wrap_loop(self, task_id: str) -> None:
        while True:
            with self._wrap_lock:
                batch = self._wrap_pending.pop(task_id, [])
                if not batch:
                    if self._wrap_workers.get(task_id) is threading.current_thread():
                        self._wrap_workers.pop(task_id, None)
                    return
            self._wrap_slots.acquire()
            try:
                self._run_wrap(task_id, batch)
            finally:
                self._wrap_slots.release()

    def _run_wrap(self, task_id: str, batch: list) -> None:
        hang = self.hang_store.get(task_id)
        if hang is None or hang.status == "cancelled":
            return
        skill = self.wrap_skill
        if skill is None:
            return
        try:
            result = skill.wrap_hang(hang, batch)
        except Exception:
            logger.exception("tool wrap skill failed")
            result = None
        if result is None:
            terminal = next(
                (
                    item.result
                    for item in reversed(batch)
                    if item.kind is FeedbackKind.RESULT and item.result is not None
                ),
                None,
            )
            if terminal is None:
                return
            text = (terminal.error or terminal.status.value).strip()
            if not text:
                return
            self.hang_store.set_wrap(
                task_id,
                visible=True,
                summary=text,
                wrap_meta={"source": "fallback"},
                terminal=True,
            )
            return
        terminal = any(item.kind is FeedbackKind.RESULT for item in batch)
        summary = (result.summary or "").strip()
        # 终态包装若空摘要，用引擎 RESULT 正文兜底，避免收口后仍留着「还在查」的中途句。
        if terminal and not summary:
            engine_result = next(
                (
                    item.result
                    for item in reversed(batch)
                    if item.kind is FeedbackKind.RESULT and item.result is not None
                ),
                None,
            )
            if engine_result is not None:
                summary = (
                    engine_result.summary
                    or engine_result.error
                    or engine_result.status.value
                ).strip()
        self.hang_store.set_wrap(
            task_id,
            visible=bool(result.visible) and bool(summary),
            summary=summary,
            wrap_meta={"model_tag": getattr(skill, "model_tag", "")},
            terminal=terminal,
        )

    def list_visible(
        self, subject_id: str, object_id: str
    ) -> tuple[VisibleToolItem, ...]:
        items: list[VisibleToolItem] = []
        for record in self.intake_store.list_for(subject_id, object_id):
            if record.status != "failed":
                continue
            if record.delivered_at is not None:
                continue  # 策划失败句也已交付进片场 → 不再走 tool_input
            items.append(
                VisibleToolItem(
                    id=record.intake_id,
                    summary=(record.plan_error or "策划失败").strip(),
                    subject_id=record.subject_id,
                    object_id=record.object_id,
                    kind="plan_failed",
                    updated_at=record.updated_at,
                )
            )
        for hang in self.hang_store.list_for(subject_id, object_id):
            if hang.status == "cancelled" or not hang.visible:
                continue
            if hang.delivered_at is not None:
                continue  # 已写进片场 → 不再走 tool_input，避免同一句出现两次
            summary = (hang.summary or "").strip()
            if not summary:
                continue
            items.append(
                VisibleToolItem(
                    id=hang.task_id,
                    summary=summary,
                    subject_id=hang.subject_id,
                    object_id=hang.object_id,
                    kind="hang",
                    updated_at=hang.updated_at,
                )
            )
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(items[:OPEN_LIST_CAP])

    def list_hot_state(
        self,
        subject_id: str,
        object_id: str,
        *,
        now: datetime | None = None,
        ttl_s: float = HOT_STATE_TTL_S,
    ) -> tuple[ToolHotItem, ...]:
        """该对象工具热状态：进行中 + 终态未满 ``ttl_s``。

        与 ``list_visible`` 不同：不论是否已交付进片场，只要还在热窗内就给 05 看，
        用来判断要不要再开工具。无则空。
        """
        stamp = now or utc_now()
        items: list[ToolHotItem] = []
        for record in self.intake_store.list_for(subject_id, object_id):
            if record.status != "failed":
                continue
            age = (stamp - record.updated_at).total_seconds()
            if age > ttl_s:
                continue
            items.append(
                ToolHotItem(
                    id=record.intake_id,
                    phase="failed",
                    source="plan_failed",
                    need=record.need,
                    summary=(record.plan_error or "策划失败").strip(),
                    updated_at=record.updated_at,
                )
            )
        for hang in self.hang_store.list_for(subject_id, object_id):
            if hang.status == "cancelled":
                continue
            if hang.status == "open":
                items.append(
                    ToolHotItem(
                        id=hang.task_id,
                        phase="running",
                        source="hang",
                        command=hang.command,
                        need=hang.need,
                        summary=(hang.summary or hang.note or "").strip(),
                        updated_at=hang.updated_at,
                    )
                )
                continue
            if hang.status != "notified":
                continue
            age = (stamp - hang.updated_at).total_seconds()
            if age > ttl_s:
                continue
            items.append(
                ToolHotItem(
                    id=hang.task_id,
                    phase=hang_hot_phase(hang),
                    source="hang",
                    command=hang.command,
                    need=hang.need,
                    summary=(hang.summary or "").strip(),
                    updated_at=hang.updated_at,
                )
            )
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(items[:HOT_STATE_CAP])

    def _estimate_blurb(self, hang) -> str:
        """启动时估价（benefit / downside），写入工具描述，不单开字段。

        跳过「benefit=整段 need」和占位 downside，避免描述里堆无用字。
        """
        need = (getattr(hang, "need", "") or "").strip()
        for item in tuple(getattr(hang, "feedback", ()) or ()):
            if getattr(item, "kind", None) is not FeedbackKind.ESTIMATE:
                continue
            estimate = getattr(item, "estimate", None)
            if estimate is None:
                continue
            benefit = str(getattr(estimate, "benefit", "") or "").strip()
            downside = str(getattr(estimate, "downside", "") or "").strip()
            bits: list[str] = []
            if benefit and benefit != need and benefit != PLACEHOLDER_DOWNSIDE:
                bits.append(benefit)
            if (
                downside
                and downside != PLACEHOLDER_DOWNSIDE
                and "占位估价" not in downside
            ):
                bits.append(f"风险：{downside}")
            return "；".join(bits)
        return ""

    def _tool_description_for(
        self,
        *,
        command: str,
        kind: str,
        meta: Mapping[str, str] | None,
        hang: Any | None = None,
    ) -> str:
        """名称旁必有的一句能力说明。可附多步序号与启动估价。"""
        meta = meta or {}
        intent = str(meta.get("tool_intent") or "").strip()
        if kind == "create" and intent:
            base = intent
        elif kind == "propose":
            base = intent or "提案：尚未执行"
        else:
            cmd = (command or "").strip()
            base = ""
            if cmd:
                try:
                    from .catalog import engine_tools

                    for item in engine_tools(getattr(self, "engine", None)):
                        if str(item.get("name") or "").strip() == cmd:
                            base = str(item.get("description") or "").strip() or cmd
                            break
                except Exception:
                    pass
                if not base:
                    base = cmd
            else:
                base = "外部工具"
        if hang is not None:
            total = int(getattr(hang, "plan_total", 0) or 0)
            index = int(getattr(hang, "plan_index", 0) or 0)
            if total > 1:
                step = index if index > 0 else "?"
                base = f"{base}；多步计划第{step}/{total}步"
            est = self._estimate_blurb(hang)
            if est:
                base = f"{base}；启动估价：{est}" if base else f"启动估价：{est}"
        return base

    def _hang_used_at(self, hang) -> datetime:
        stages = tuple(getattr(hang, "stages", ()) or ())
        if stages:
            created = getattr(stages[0], "created_at", None)
            if isinstance(created, datetime):
                return created
        return getattr(hang, "updated_at", None) or utc_now()

    def _is_invalidated(self, meta: Mapping[str, Any] | None) -> bool:
        raw = str((meta or {}).get("invalidated") or "").strip().lower()
        return raw in {"1", "true", "yes"}

    def _is_dedupe_plan_error(self, message: str) -> bool:
        text = (message or "").strip()
        return any(marker in text for marker in _DEDUP_PLAN_MARKERS)

    def reap_stale(
        self,
        subject_id: str,
        object_id: str,
        *,
        now: datetime | None = None,
        planning_ttl_s: float = PLANNING_TTL_S,
        open_ttl_s: float = HOT_STATE_TTL_S,
    ) -> tuple[int, int]:
        """清理卡死账本：超时仍 received 的策划、过久无更新的 open 记挂。

        返回 ``(策划超时条数, 注销的 open 条数)``。供组装与 intake 前调用，
        避免「半天前还在策划 / 还在使用中」挡住重试。
        """
        stamp = now or utc_now()
        failed_planning = 0
        cancelled_open = 0
        for record in self.intake_store.list_for(subject_id, object_id):
            if record.status != "received":
                continue
            if (record.task_id or "").strip():
                continue
            age = (stamp - record.updated_at).total_seconds()
            if age <= planning_ttl_s:
                continue
            self._fail_intake(record.intake_id, "策划超时未完成")
            failed_planning += 1
        for hang in self.hang_store.list_for(subject_id, object_id):
            if hang.status != "open":
                continue
            # 引擎已交 RESULT，但包装未收口（旧逻辑 / 包装线程中断）→ 立刻 notified，
            # 否则会一直「使用中」，05 误以为汇率等还在跑。
            last_result = next(
                (
                    item.result
                    for item in reversed(hang.feedback)
                    if item.kind is FeedbackKind.RESULT and item.result is not None
                ),
                None,
            )
            if last_result is not None:
                # 优先从进度事实里拼实质输出（汇率行 / 检索 JSON），
                # 不要用探路目录清单，也不要一直留着「还在查询中」的中途句。
                from jshi.tool.pi_engine import (
                    _compact_shell_summary,
                    _shell_output_is_exploration,
                )

                parts: list[str] = []
                for item in hang.feedback:
                    if item.kind is not FeedbackKind.PROGRESS or item.progress is None:
                        continue
                    partial = (item.progress.partial or "").strip()
                    if not partial or _shell_output_is_exploration(partial):
                        continue
                    if partial not in parts:
                        parts.append(partial)
                text = _compact_shell_summary(parts, limit=1500) if parts else ""
                if not text:
                    text = (hang.summary or "").strip()
                if not text or _shell_output_is_exploration(text):
                    text = (
                        last_result.summary
                        or last_result.error
                        or "工具已跑完"
                    ).strip()
                    if _shell_output_is_exploration(text):
                        text = "工具已跑完，终态摘要不完整"
                self.hang_store.set_wrap(
                    hang.task_id,
                    visible=bool(text),
                    summary=text,
                    wrap_meta={
                        **dict(hang.wrap_meta or {}),
                        "source": "reap_result",
                    },
                    terminal=True,
                )
                continue
            age = (stamp - hang.updated_at).total_seconds()
            if age <= open_ttl_s:
                continue
            if self.cancel(hang.task_id):
                cancelled_open += 1
        return failed_planning, cancelled_open

    def list_tool_related_entries(
        self,
        subject_id: str,
        object_id: str,
        *,
        now: datetime | None = None,
        ttl_s: float = HOT_STATE_TTL_S,
    ) -> tuple[dict[str, Any], ...]:
        """组装【工具相关】条目（程序侧结构，再交给 format_tool_related）。

        含：策划中（intake received、205 在跑）、使用中记挂、近时终态、
        近时取消、策划失败。已被 ``invalidate`` 的不出现。
        近期完成按工具名归并：同一工具只保留最后一次成功与最后一次失败。
        组装前先 ``reap_stale``，清掉超时策划与僵尸 open。
        """
        stamp = now or utc_now()
        self.reap_stale(subject_id, object_id, now=stamp)
        entries: list[dict[str, Any]] = []

        for record in self.intake_store.list_for(subject_id, object_id):
            if self._is_invalidated(record.meta):
                continue
            # 策划中：交接已建、205 尚在跑（或排队），还没有记挂。
            if record.status == "received" and not (record.task_id or "").strip():
                age = (stamp - record.updated_at).total_seconds()
                if age > PLANNING_TTL_S:
                    continue
                entries.append(
                    {
                        "id": record.intake_id,
                        "section": "in_use",
                        "status": "使用中",
                        "need": record.need,
                        "tool_name": "策划中",
                        "tool_description": "正在形成可执行步骤（策划在跑，尚无工具名）",
                        "used_at": record.updated_at,
                        "new_info": "",
                    }
                )
                continue
            if record.status != "failed":
                continue
            age = (stamp - record.updated_at).total_seconds()
            if age > ttl_s:
                continue
            error = (record.plan_error or "策划失败").strip()
            if self._is_dedupe_plan_error(error):
                name = "未新开"
                desc = "与在办事项重复，未另开一本"
            elif "超时" in error:
                name = "策划"
                desc = "策划超时未完成"
            else:
                name = "策划"
                desc = "未能形成可执行步骤"
            entries.append(
                {
                    "id": record.intake_id,
                    "section": "recent",
                    "status": "失败",
                    "need": record.need,
                    "tool_name": name,
                    "tool_description": desc,
                    "used_at": record.updated_at,
                    "final_result": error,
                }
            )

        for hang in self.hang_store.list_for(subject_id, object_id):
            if self._is_invalidated(hang.meta):
                continue
            name = (hang.command or "").strip()
            if hang.kind == "propose" and name:
                name = f"{name}（尚未执行）"
            elif hang.kind == "propose":
                name = "提案（尚未执行）"
            elif hang.kind == "create" and not name:
                name = str((hang.meta or {}).get("tool_intent") or "").strip() or "创建中"
            desc = self._tool_description_for(
                command=hang.command,
                kind=hang.kind,
                meta=hang.meta,
                hang=hang,
            )
            if hang.kind == "propose" and "尚未执行" not in desc:
                desc = f"{desc}（尚未执行）" if desc else "提案：尚未执行"
            used_at = self._hang_used_at(hang)

            if hang.status == "cancelled":
                age = (stamp - hang.updated_at).total_seconds()
                if age > ttl_s:
                    continue
                final = "已取消，不再执行"
                if "超时" in str((hang.meta or {}).get("cancel_reason") or ""):
                    final = "超时未更新，已当作中断取消"
                # cancel() 不写 meta；用年龄启发式：若刚被 reap，summary 可能空
                entries.append(
                    {
                        "id": hang.task_id,
                        "section": "recent",
                        "status": "失败",
                        "need": hang.need,
                        "tool_name": name or hang.command or "工具",
                        "tool_description": desc,
                        "used_at": used_at,
                        "final_result": final,
                    }
                )
                continue

            if hang.status == "open":
                new_info = ""
                if hang.visible and hang.delivered_at is None:
                    new_info = (hang.summary or "").strip()
                entries.append(
                    {
                        "id": hang.task_id,
                        "section": "in_use",
                        "status": "使用中",
                        "need": hang.need,
                        "tool_name": name or hang.command or "工具",
                        "tool_description": desc,
                        "used_at": used_at,
                        "new_info": new_info,
                    }
                )
                continue

            if hang.status != "notified":
                continue
            age = (stamp - hang.updated_at).total_seconds()
            if age > ttl_s:
                continue
            phase = hang_hot_phase(hang)
            entries.append(
                {
                    "id": hang.task_id,
                    "section": "recent",
                    "status": _RELATED_STATUS.get(phase, "已完成"),
                    "need": hang.need,
                    "tool_name": name or hang.command or "工具",
                    "tool_description": desc,
                    "used_at": used_at,
                    "final_result": (hang.summary or "").strip(),
                }
            )

        entries = dedupe_recent_tool_entries(entries)
        entries.sort(
            key=lambda item: item.get("used_at") or stamp,
            reverse=True,
        )
        return tuple(entries[:HOT_STATE_CAP])

    def invalidate(
        self, item_id: str, *, reason: str = ""
    ) -> bool:
        """200 主动失效：该条不再进入【工具相关】（主流程下一轮组装即清掉）。

        记挂与交接（策划失败 / 策划中）均可。不改写片场；只标 ``meta.invalidated``。
        """
        found = False
        reason_text = (reason or "").strip()[:80]
        hang = self.hang_store.get(item_id)
        if hang is not None:
            meta = dict(hang.meta)
            meta["invalidated"] = "1"
            if reason_text:
                meta["invalidate_reason"] = reason_text
            patched = self.hang_store.patch_meta(item_id, meta)
            found = patched is not None
        intake = self.intake_store.get(item_id)
        if intake is not None:
            meta = dict(intake.meta)
            meta["invalidated"] = "1"
            if reason_text:
                meta["invalidate_reason"] = reason_text
            patched = self.intake_store.update(item_id, meta=meta)
            found = found or patched is not None
        return found

    def format_tool_related_block(
        self,
        subject_id: str,
        object_id: str,
        *,
        now: datetime | None = None,
        ttl_s: float = HOT_STATE_TTL_S,
    ) -> str:
        """该对象【工具相关】全文；空则 ``\"\"``。"""
        return format_tool_related(
            self.list_tool_related_entries(
                subject_id, object_id, now=now, ttl_s=ttl_s
            ),
            now=now,
        )

    def pending_for_scene(
        self, subject_id: str, object_id: str
    ) -> tuple[VisibleToolItem, ...]:
        """该对象可见、且尚未送入主流程的条目（旧名保留）。"""
        pending = list(self.list_visible(subject_id, object_id))
        pending.sort(key=lambda item: item.updated_at)
        return tuple(pending)

    def mark_main_seen(
        self, subject_id: str, object_id: str
    ) -> tuple[str, ...]:
        """本轮【工具相关】已交给 05：标记可见句已送入主流程，不写片场。"""
        delivered: list[str] = []
        for item in self.pending_for_scene(subject_id, object_id)[:SCENE_BLOCK_CAP]:
            if item.kind == "plan_failed":
                marked = self.intake_store.set_delivered(item.id, block="")
            else:
                marked = self.hang_store.set_delivered(item.id, block="")
            if marked is None:
                continue
            delivered.append(item.id)
        return tuple(delivered)

    def deliver_to_scene(
        self, subject_id: str, object_id: str
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """兼容旧名：只标记已送入主流程，不再生成片场「（我知道）」块。

        返回 ``(空块元组, 已标记 id)``。
        """
        delivered = self.mark_main_seen(subject_id, object_id)
        return (), delivered

    def delivered_at(self, item_id: str):
        """这条送入主流程的时间（记挂或交接）。"""
        hang = self.hang_store.get(item_id)
        if hang is not None and hang.delivered_at is not None:
            return hang.delivered_at
        record = self.intake_store.get(item_id)
        if record is not None and record.delivered_at is not None:
            return record.delivered_at
        return None

    def write_back_response(
        self, task_ids: tuple[str, ...], response: Mapping[str, Any]
    ) -> None:
        """把 05 这一轮的回应回写进对应记挂（只增）。"""
        for task_id in task_ids:
            self.hang_store.set_response(task_id, response)

    def revoke_delivery(self, item_id: str) -> None:
        """撤销「已送入主流程」标记。"""
        self.hang_store.clear_delivered(item_id)
        self.intake_store.clear_delivered(item_id)

    def cancel(self, item_id: str) -> bool:
        found = False
        self._proposed_requests.pop(item_id, None)
        hang = self.hang_store.get(item_id)
        if hang is not None:
            self._proposed_requests.pop(hang.task_id, None)
            self._create_tasks.discard(hang.task_id)
            self.hang_store.append_stage(
                hang.task_id,
                stage_event(
                    hang.task_id,
                    PHASE_CANCEL,
                    kind=hang.kind,
                    status="cancelled",
                    command=hang.command,
                ),
            )
            self.runner.cancel(item_id)
            self.hang_store.cancel(item_id)
            found = True
            twin = self.intake_store.get_by_task_id(item_id)
            if twin is not None:
                self.intake_store.cancel(twin.intake_id)
        intake = self.intake_store.get(item_id)
        if intake is not None:
            self.intake_store.cancel(item_id)
            found = True
            if intake.task_id:
                self._create_tasks.discard(intake.task_id)
                self.runner.cancel(intake.task_id)
                self.hang_store.cancel(intake.task_id)
        return found

    def drain_planning_for_tests(self, timeout: float = 5.0) -> None:
        with self._lock:
            threads = list(self._planner_threads)
            self._planner_threads.clear()
        for thread in threads:
            thread.join(timeout)

    def drain_for_tests(self, timeout: float = 5.0) -> None:
        """等后台线程干净（测试用）。

        计划调度会「一步完成 → 在线程里再起下一步」，单次快照会漏掉那些后起的
        线程，所以收几轮：每轮把当时在册的线程都 join 干净。
        """
        for _ in range(4):
            self.drain_planning_for_tests(timeout)
            self.runner.drain_for_tests(timeout)
            with self._wrap_lock:
                threads = list(self._wrap_threads)
                self._wrap_threads.clear()
            for thread in threads:
                thread.join(timeout)
