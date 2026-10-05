"""200 对外口：intake / list_visible / cancel。

主流程与 03 只调这三口。策划在后台；发出 ToolRequest 之后才建记挂。
"""

from __future__ import annotations

import json
import logging
import math
import threading
from urllib.error import URLError
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
    PROGRESS_NOTE_MAX_CHARS,
    is_stage_fact,
    rule_wrap_from_item,
    truncate_note,
)
from .intake import IntakeRecord, IntakeStore
from .metrics import ToolMetricsStore
from .work import WorkIndex
from .reuse import ReusePolicy
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
TIMEOUT_USE_S = 180.0
TIMEOUT_AGENT_LOOP_S = 600.0
TIMEOUT_CREATE_S = 600.0
TIMEOUT_MIN_S = 60.0
TIMEOUT_MAX_S = 900.0
TIMEOUT_ESTIMATE_FACTOR = 3.0


def resolve_timeout_s(kind: str, estimate: Mapping[str, Any] | None = None) -> float:
    """按动作分档，并可被 time_est_ms 放大（不超过封顶）。"""
    base = {"create": TIMEOUT_CREATE_S, "agent_loop": TIMEOUT_AGENT_LOOP_S}.get(kind, TIMEOUT_USE_S)
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
# 使用中记挂：引擎多久没有进度就当作中断；另有从建档算起的总时长上限。
OPEN_IDLE_TTL_S = HOT_STATE_TTL_S
OPEN_MAX_LIFETIME_S = 24 * 3600
HOT_STATE_CAP = 12
# 未消化结果：05 没有标明已消化时每轮再送；超过这个时长后只按话题召回。
UNCONSUMED_TTL_S = 24 * 3600
# 按话题从已消化旧结果里召回：至少命中几个不同的实词、最多几条、合计多少字。
# 字数只约束这些可有可无的旧结果；未消化的新结果与使用中的条目整条送入，不按字数截。
RELEVANT_MIN_TERMS = 2
RELEVANT_CAP = 3
TOOL_RELATED_CHARS = 8000
# 话题词里不算实词的字：代词、虚词、泛用动词。含这些字的双字组不参与匹配。
_TOPIC_STOP_CHARS = frozenset(
    "的了吗呢吧啊呀嘛哦哈么着过得地和与及或而且就都也还再又才只很太更最"
    "我你您他她它们咱这那哪谁啥什怎样么些个位种件次回下上里中外前后来去"
    "是在有没不无别把被给让叫对向从到于为以所之其可能会要想该应请帮说讲"
    "看查找问告诉知道一二两几多少点儿吧好行嗯呃喂"
)
_TOPIC_STOP_WORDS = frozenset(
    {"the", "a", "an", "is", "are", "to", "of", "and", "or", "in", "on", "for",
     "it", "me", "my", "you", "your", "what", "how", "can", "please", "again"}
)


def topic_terms(text: str) -> frozenset[str]:
    """话题匹配用的实词：去掉单字与含虚词/代词的双字组。"""
    from .discovery import tokenize

    terms: set[str] = set()
    for term in tokenize(text):
        if term.isascii():
            if len(term) >= 3 and term not in _TOPIC_STOP_WORDS:
                terms.add(term)
            continue
        if len(term) < 2 or any(char in _TOPIC_STOP_CHARS for char in term):
            continue
        terms.add(term)
    return frozenset(terms)


def _topic_match(hits: frozenset[str], need_hits: int) -> bool:
    """命中词数够，且汉字双字组要覆盖至少 3 个不同的字（「天天」「天气」只算一个词）。"""
    if len(hits) < need_hits or not hits:
        return False
    if need_hits < RELEVANT_MIN_TERMS:
        return True
    ascii_hits = sum(1 for term in hits if term.isascii())
    chars = {char for term in hits if not term.isascii() for char in term}
    return ascii_hits >= RELEVANT_MIN_TERMS or len(chars) + 2 * ascii_hits >= 3
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
    "partial": "部分完成",
}
_RELATED_STATUS = {
    "running": "使用中",
    "done": "已完成",
    "failed": "失败",
    "partial": "部分完成",
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
    """近期完成：只归并同一工具、同一需求和参数的重复执行。"""
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
    best_ok: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    best_fail: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for item in recent:
        params = item.get("params")
        key = (
            _normalize_tool_related_key(str(item.get("tool_name") or "")),
            " ".join(str(item.get("need") or "").lower().split()),
            json.dumps(params if isinstance(params, Mapping) else {}, sort_keys=True, default=str),
            str(item.get("id")) if item.get("work_open") else "",
        )
        status = str(item.get("status") or "").strip()
        if status == "已完成":
            if key not in best_ok:
                best_ok[key] = item
        elif status in {"失败", "部分完成"}:
            if key not in best_fail:
                best_fail[key] = item
    kept = list(best_ok.values()) + list(best_fail.values())
    kept.sort(key=_entry_used_at, reverse=True)
    return in_use + kept


def select_tool_related_entries(
    entries: Sequence[Mapping[str, Any]],
    *,
    query: str,
    now: datetime,
    ttl_s: float,
) -> tuple[dict[str, Any], ...]:
    """从原记挂账本挑本轮上下文。

    - 使用中的条目与 05 尚未标明已消化的结果：整条送入，不按字数截。
    - 已消化的旧结果：只在当前话题命中至少 ``RELEVANT_MIN_TERMS`` 个实词时召回，
      最多 ``RELEVANT_CAP`` 条、合计不超过 ``TOOL_RELATED_CHARS`` 字。
    ``ttl_s`` 保留作兼容参数；已消化结果不再按时间自动重现。
    """
    del ttl_s
    terms = topic_terms(query)
    need_hits = min(RELEVANT_MIN_TERMS, len(terms))
    unseen: list[dict[str, Any]] = []
    relevant: list[tuple[int, dict[str, Any]]] = []
    active: list[dict[str, Any]] = []
    for raw in entries:
        item = dict(raw)
        if item.get("section") == "in_use":
            active.append(item)
            continue
        if item.get("section") != "recent":
            continue
        result_at = item.get("updated_at") or _entry_used_at(item)
        fresh = (now - result_at).total_seconds() <= UNCONSUMED_TTL_S
        if item.get("work_open") or (item.get("delivered_at") is None and fresh):
            unseen.append(item)
            continue
        if not need_hits:
            continue
        basis = " ".join(
            str(item.get(key) or "") for key in ("need", "tool_name", "tool_description")
        )
        hits = terms & topic_terms(basis)
        if _topic_match(hits, need_hits):
            relevant.append((len(hits), item))

    unseen.sort(key=lambda item: (
        item.get("delivered_at") is not None,
        item.get("delivered_at") or _entry_used_at(item),
    ))  # Unseen first; rotate already-presented open-work material at the cap.
    active.sort(key=_entry_used_at, reverse=True)
    relevant.sort(key=lambda pair: (pair[0], _entry_used_at(pair[1])), reverse=True)
    selected: list[dict[str, Any]] = []
    for item in unseen[:4] + active[:3] + unseen[4:] + active[3:]:
        if len(selected) >= HOT_STATE_CAP:
            return tuple(selected)
        selected.append(item)
    used_chars = 0
    for _score, item in relevant[:RELEVANT_CAP]:
        if len(selected) >= HOT_STATE_CAP:
            break
        size = sum(
            len(str(item.get(key) or ""))
            for key in ("need", "tool_name", "tool_description", "final_result")
        )
        if used_chars + size > TOOL_RELATED_CHARS:
            continue
        selected.append(item)
        used_chars += size
    return tuple(selected)


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
        if status is ToolStatus.PARTIAL:
            return "partial"
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
    entries: Sequence[Mapping[str, Any]], *, now: datetime | None = None,
    task_codes: Mapping[str, str] | None = None,
) -> str:
    """Tool-side rule brief; original results remain in task storage.

    Active/undelivered tasks are never dropped for a total character budget.
    Repeated lines/descriptions are removed; long bodies retain a source reference.
    """
    from jshi.models.prompt import relative_time_label
    selected = [item for item in entries if item.get("section") in {"in_use", "recent"}]
    if not selected:
        return ""
    codes = task_codes or {}
    lines = ["【工具相关】", "工具工作简报，不是对方原话；任务ID可用于结果处理，完整原文保存在对应任务。"]
    def brief(value, cap):
        raw = str(value or "").strip()
        unique = list(dict.fromkeys(line.strip() for line in raw.splitlines() if line.strip()))
        text = "\n".join(unique)
        return text if len(text) <= cap else text[:cap] + "…（原文见对应任务）"
    for section, title in (("in_use", "使用中的工具"), ("recent", "近期已结束的工具")):
        rows = [item for item in selected if item["section"] == section]
        if not rows:
            continue
        lines.append("# " + title)
        for item in rows:
            seen = set()
            def add(label, value, cap=300):
                text = brief(value, cap)
                if text and text not in seen:
                    lines.append(label + "：" + text)
                    seen.add(text)
            oid = str(item.get("id") or "")
            if oid:
                lines.append("任务ID：" + codes.get(oid, oid))
            add("需求", item.get("need"))
            add("工具名称", item.get("tool_name"), 120)
            desc = str(item.get("tool_description") or "")
            if desc != str(item.get("tool_name") or ""):
                add("工具描述", desc, 120)
            used = item.get("used_at")
            if isinstance(used, datetime):
                add("使用时间", relative_time_label(used, now=now))
            add("状态", item.get("status"))
            add("新的信息", item.get("new_info"), 2000)
            add("最终的结果", item.get("final_result"), 2000)
            add("结束原因", item.get("end_reason"), 500)
            if item.get("work_open"):
                lines.append("事项：尚未交付完毕，已有材料保留待用")
            if item.get("work_ids"):
                add("事项ID", ", ".join(item["work_ids"]))
            needs = [n for n in item.get("work_needs", ()) if n != item.get("need")]
            add("所属事项需求", "；".join(dict.fromkeys(needs)))
            add("尚未完成的步骤", ", ".join(item.get("pending_steps") or ()))
            add("计划进度", item.get("plan_progress"))
            add("结果处理", item.get("handling"))
            add("复用来源任务", item.get("reused_from"))
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
    if request.meta.get("reused_from"):
        meta["reused_from"] = str(request.meta["reused_from"])
    if plan_tag:
        meta["plan_model_tag"] = plan_tag
    create = request.create
    feedback_plan = (
        create.feedback_plan
        if create is not None
        else request.meta.get("feedback_plan")
    )
    if isinstance(feedback_plan, Mapping) and feedback_plan:
        meta["feedback_plan"] = json.dumps(dict(feedback_plan), ensure_ascii=False)
    expected_result = (request.expected_result or "").strip()
    if expected_result:
        meta["expected_result"] = expected_result
    if create is not None:
        meta["fulfill_after_create"] = "1" if create.fulfill_after_create else "0"
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


def _terminal_fallback_summary(result: ToolResult) -> str:
    """包装模型不可用时，优先保留完整终态结果。"""
    payload = result.result
    if isinstance(payload, Mapping):
        content = payload.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if content is not None:
            return json.dumps(content, ensure_ascii=False, default=str)
        if payload:
            return json.dumps(dict(payload), ensure_ascii=False, default=str)
    summary = (result.summary or "").strip()
    if summary:
        return summary
    error = (result.error or "").strip()
    if error:
        return error
    return result.status.value


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
        reuse_policies: Mapping[str, ReusePolicy] | None = None,
    ) -> None:
        self.hang_store = hang_store
        self.runner = runner
        self.work_index = WorkIndex(hang_store.path.parent / "tool_work.jsonl")
        self.reuse_policies = dict(reuse_policies or {})
        self._session_tasks: set[str] = set()
        policy_path = hang_store.path.parent / "tool_reuse.json"
        if policy_path.exists():
            try:
                policies = json.loads(policy_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                policies = {}
            if isinstance(policies, Mapping):
                for command, raw in policies.items():
                    policy = self._parse_reuse_policy(raw)
                    if policy is not None and command not in self.reuse_policies:
                        self.reuse_policies[command] = policy
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
        self._plan_lock = threading.RLock()
        self._plan_intake_ids: dict[str, str] = {}
        self._plan_task_to_plan: dict[str, str] = {}
        self._plan_task_to_step: dict[str, str] = {}
        self._plan_step_tasks: dict[str, dict[str, str]] = {}
        self._plan_agent_tasks: dict[str, str] = {}
        self._plan_completed: dict[str, set[str]] = {}
        self._plan_status: dict[str, str] = {}
        self._plan_note: dict[str, str] = {}
        self.runner.on_feedback = self._on_engine_feedback
        if hasattr(self.planner, "material_loader") and self.planner.material_loader is None:
            self.planner.material_loader = lambda intake: format_tool_related([
                entry for entry in self.list_tool_related_entries(
                    intake.subject_id, intake.object_id, query=intake.need)
                if entry.get("final_result") and self.hang_store.get(str(entry["id"]))
            ])

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
        refresh_reason: str = "",
        work_id: str = "",
        step_id: str = "",
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
        intake_meta = {}
        row = self.work_index.get(work_id)
        if (row and not row["closed"] and row["subject_id"] == subject_id
                and row["object_id"] == object_id):
            known_steps = {step["step_id"] for step in (row.get("plan") or {}).get("steps", ())}
            if not step_id or step_id in known_steps:
                intake_meta.update(work_id=work_id, step_id=step_id)
        if refresh_reason.strip():
            intake_meta["refresh_reason"] = refresh_reason.strip()
        if intake_meta:
            record = self.intake_store.update(
                record.intake_id, meta=intake_meta
            ) or record
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
        except Exception as exc:
            if isinstance(exc, URLError):
                logger.error("tool planner failed: %s", exc)
            else:
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
        """造工具完成后刷新目录；查询型原需求在后台接着使用新工具。

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
        hang = self.hang_store.get(task_id)
        if (
            not result.ideal
            or hang is None
            or hang.plan_id
            or hang.meta.get("fulfill_after_create") != "1"
        ):
            return
        thread = threading.Thread(
            target=self._continue_after_create,
            args=(task_id,),
            name=f"tool-created-use-{task_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._planner_threads.append(thread)
        thread.start()

    def _continue_after_create(self, task_id: str) -> None:
        hang = self.hang_store.get(task_id)
        original = self.intake_store.get_by_task_id(task_id)
        if hang is None or original is None or hang.status == "cancelled":
            return
        from .catalog import engine_tools

        name = (hang.command or "").strip()
        candidates = {name, f"skill:{name}"}
        catalog = engine_tools(self.runner.module.engine)
        tool = next(
            (entry for entry in catalog if str(entry.get("name") or "").strip() in candidates),
            None,
        )
        followup = self.intake_store.create(
            subject_id=original.subject_id,
            object_id=original.object_id,
            activity_id=original.activity_id,
            need=original.need,
            verbal=original.verbal,
            field_ref=original.field_ref,
            origin=original.origin,
        )
        followup = self.intake_store.update(
            followup.intake_id, meta={"created_from": task_id, "work_id": original.intake_id,
                                     "refresh_reason": original.meta.get("refresh_reason", "")}
        ) or followup
        if tool is None:
            self._fail_intake(
                followup.intake_id,
                "工具文件已创建，但新工具尚未出现在引擎目录，原需求未执行",
            )
            return
        try:
            origin = ToolOrigin(original.origin)
        except ValueError:
            origin = ToolOrigin.EXTERNAL_05
        tool_meta = tool.get("meta")
        tool_meta = tool_meta if isinstance(tool_meta, Mapping) else {}
        feedback_plan = tool_meta.get("feedback_plan")
        if not isinstance(feedback_plan, Mapping):
            feedback_plan = {}
        request = ToolRequest(
            subject_id=original.subject_id,
            activity_id=original.activity_id,
            origin=origin,
            need=original.need,
            template="generic",
            command=str(tool["name"]),
            field_ref=original.field_ref,
            expected_result=str(hang.meta.get("expected_output") or ""),
            meta={"feedback_plan": dict(feedback_plan)},
        )
        self._launch(followup, request)

    @staticmethod
    def _parse_reuse_policy(raw: Any) -> ReusePolicy | None:
        if not isinstance(raw, Mapping) or raw.get("read_only") is not True:
            return None
        try:
            keys = raw["required_params"]
            dates = raw.get("date_range")
            if (not isinstance(keys, list) or not keys
                    or not all(isinstance(k, str) and k for k in keys)):
                return None
            if dates is not None and (not isinstance(dates, list) or len(dates) != 2
                                      or not all(isinstance(k, str) and k for k in dates)):
                return None
            ttl = float(raw["ttl_s"])
            if isinstance(raw["ttl_s"], bool) or not math.isfinite(ttl) or ttl <= 0:
                return None
            if dates and (dates[0] == dates[1] or not set(dates).issubset(keys)):
                return None
            return ReusePolicy(ttl, tuple(keys), tuple(dates) if dates else None)
        except (KeyError, TypeError, ValueError):
            return None

    def _find_reusable(self, intake: IntakeRecord, request: ToolRequest):
        if (request.ask is not AskMode.EXECUTE or request.meta.get("refresh_reason")
                or intake.meta.get("refresh_reason")):
            return None
        policy = self.reuse_policies.get(request.command)
        if policy is None:
            return None
        now = utc_now()
        for hang in self.hang_store.list_for(intake.subject_id, intake.object_id):
            if (hang.command == request.command and hang.kind == "use"
                    and hang.status == "notified" and not self._is_invalidated(hang.meta)
                    and policy.match(hang, request, now)):
                return hang
        return None

    def _work_execution_done(self, row: Mapping[str, Any]) -> bool:
        plan = row.get("plan") or {}
        tasks = row["tasks"]
        if plan.get("mode") == "agent_loop":
            if "agent" not in tasks:
                return False
        elif plan and any(step["step_id"] not in tasks for step in plan.get("steps", ())):
            return False
        if not tasks:
            return False
        for task_id in tasks.values():
            hang = self.hang_store.get(task_id)
            if hang is None:
                intake = self.intake_store.get(task_id)
                if intake is None or intake.status != "failed":
                    return False
                continue
            if (hang.kind == "propose" or hang.status == "open"
                    or not any(f.kind is FeedbackKind.RESULT for f in hang.feedback)):
                return False
            if hang.kind == "create" and hang.meta.get("fulfill_after_create") == "1":
                if not any((child := self.hang_store.get(t)) and child.kind == "use"
                           for t in tasks.values()):
                    return False
        return True

    def handle_results(self, subject_id: str, object_id: str, handling: Sequence[Mapping[str, Any]],
                       *, selected_ids: Sequence[str], reply: str, unsaid: str = "",
                       new_need: str = "") -> tuple[str, ...]:
        """Validate evidence and preserve open-work material independently of wording.

        Evidence being in reply is a provenance check, not a semantic proof that
        the whole requested deliverable is correct. Missing evidence stays deferred.
        """
        from jshi.core.tool_handling import parse_tool_handling
        accepted = []
        closure_requests = []
        for item in parse_tool_handling(handling):
            task_id = item["task_id"]
            if task_id not in selected_ids:
                continue
            hang = self.hang_store.get(task_id)
            record = hang or self.intake_store.get(task_id)
            if record is None or record.subject_id != subject_id or record.object_id != object_id:
                continue
            works = self.work_index.for_task(task_id)
            if not works:
                # Legacy tasks acquire an explicit work relationship on first handling.
                self.work_index.create(task_id, subject_id, object_id, record.need)
                self.work_index.bind(task_id, task_id)
                works = self.work_index.for_task(task_id)
            evidence = item["evidence"]
            valid = bool(evidence and evidence in reply)
            disposition = item["disposition"]
            if disposition == "dismissed" and not item["reason"]:
                disposition = "deferred"
            if disposition != "deferred" and (not valid or record.status in {"open", "received"}):
                disposition = "deferred"
            recorded = {**dict(item), "disposition": disposition}
            for row in works:
                self.work_index.handle(row["work_id"], task_id, recorded)
                if item["work_complete"] and valid and not unsaid.strip() and not new_need:
                    closure_requests.append((row["work_id"], evidence))
            if disposition != "deferred":
                accepted.append(task_id)
        for work_id, evidence in closure_requests:
            row = self.work_index.get(work_id)
            if row and self._work_execution_done(row) and all(
                row["handling"].get(task_id, {}).get("disposition") in {"answered", "dismissed"}
                for task_id in row["tasks"].values()
            ):
                self.work_index.close(work_id, evidence)
        return tuple(accepted)

    def _launch(
        self,
        intake: IntakeRecord,
        request: ToolRequest,
        *,
        plan_tag: str = "",
        plan_ctx: Mapping[str, Any] | None = None,
    ) -> str | None:
        ctx = dict(plan_ctx or {})
        if ctx.get("plan_id"):
            parent = self.intake_store.get(self._plan_intake_ids.get(str(ctx["plan_id"]), ""))
            if parent and parent.meta.get("refresh_reason"):
                request = replace(request, meta={**dict(request.meta),
                                                "refresh_reason": parent.meta["refresh_reason"]})
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
        work_id = plan_id or str(intake.meta.get("work_id") or intake.intake_id)
        step_id = str(ctx.get("step_id") or intake.meta.get("step_id") or "")
        self.work_index.create(work_id, intake.subject_id, intake.object_id, intake.need)
        with self._dedupe_lock:
            if command:
                existing = self.hang_store.find_open_by_command(
                    intake.subject_id,
                    intake.object_id,
                    command,
                    kind=kind,
                    need=request.need or intake.need,
                    params=request.params,
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
                    self.work_index.bind(work_id, existing.task_id, step_id)
                    self._register_plan_task(existing.task_id, ctx)
                    return existing.task_id
            reused = self._find_reusable(intake, request) if kind == "use" else None
            if reused is not None:
                # Reuse gets its own task identity. It must not overwrite a prior
                # plan's membership or pretend to be a new engine execution.
                request = replace(request, meta={**dict(request.meta), "reused_from": reused.task_id})
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
            self.work_index.bind(work_id, hang.task_id,
                                 "agent" if ctx.get("is_agent_loop") else step_id)
            self._session_tasks.add(hang.task_id)
        if reused is not None:
            source = next(f for f in reversed(reused.feedback)
                          if f.kind is FeedbackKind.RESULT and f.result is not None)
            result = replace(source.result, time_ms=0, cost=0, resource={}, execution=())
            feedback = ToolFeedback(request_id=hang.request_id, kind=FeedbackKind.RESULT,
                                    result=result, at=source.at,
                                    meta={"reused_from": reused.task_id})
            self.intake_store.update(intake.intake_id, status="launched",
                                     task_id=hang.task_id, request_id=hang.request_id,
                                     meta={**dict(intake.meta), "reused_from": reused.task_id})
            self.hang_store.append_feedback(hang.task_id, (feedback,))
            self.hang_store.set_wrap(hang.task_id, visible=True, summary=reused.summary,
                                     terminal=True, wrap_meta={"reused_from": reused.task_id})
            self._plan_on_result(hang.task_id, ToolStatus.OK)
            return hang.task_id
        est_meta = request.meta.get("estimate") if isinstance(request.meta, Mapping) else {}
        if not isinstance(est_meta, Mapping):
            est_meta = {}
        timeout_s = resolve_timeout_s("agent_loop" if ctx.get("is_agent_loop") else kind, est_meta)
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
        self.work_index.create(plan.plan_id, intake.subject_id, intake.object_id,
                               plan.need or intake.need, _plan_to_dict(plan))
        self.intake_store.update(intake.intake_id, status="launched",
                                 meta={**dict(intake.meta), "plan_id": plan.plan_id})
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
        for row in self.work_index.for_task(task_id):
            if row["work_id"] in self._plans:
                for step_id, bound_task in row["tasks"].items():
                    if bound_task == task_id:
                        self._plan_result_for(row["work_id"], step_id, status)

    def _plan_result_for(self, plan_id: str, step_id: str, status: ToolStatus) -> None:
        plan = self._plans.get(plan_id)
        if plan is None:
            return
        if self._plan_status.get(plan_id) in PLAN_FROZEN:
            return
        if step_id == "agent":
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
            row = self.work_index.get(plan_id)
            if row is None or not row.get("plan"):
                return False
            for task_id in row["tasks"].values():
                self.cancel(task_id)
            self.work_index.close(plan_id, "取消计划", cancelled=True)
            return True
        self._plan_status[plan_id] = PLAN_CANCELLED
        self._plan_note[plan_id] = "cancelled"
        for task_id in list(self._plan_step_tasks.get(plan_id, {}).values()):
            self.cancel(task_id)
        agent_task = self._plan_agent_tasks.get(plan_id)
        if agent_task:
            self.cancel(agent_task)
        self.work_index.close(plan_id, "取消计划", cancelled=True)
        return True

    def plan_summary(self, plan_id: str) -> dict[str, Any] | None:
        """只读汇总一个计划当前各 step 的可见状态。"""
        plan = self._plans.get(plan_id)
        if plan is None:
            row = self.work_index.get(plan_id)
            if row is None or not row.get("plan"):
                return None
            steps = []
            for step in row["plan"].get("steps", ()):
                task_id = row["tasks"].get(step["step_id"])
                hang = self.hang_store.get(task_id) if task_id else None
                steps.append({"step_id": step["step_id"], "task_id": task_id,
                              "status": ("interrupted" if hang.status == "open" else hang.status)
                              if hang else "pending", "summary": hang.summary if hang else ""})
            agent_id = row["tasks"].get("agent")
            agent = self.hang_store.get(agent_id) if agent_id else None
            done = self._work_execution_done(row)
            failed = any(
                f.result and f.result.status not in PLAN_SATISFIED_STATUSES
                for task_id in row["tasks"].values()
                for f in (self.hang_store.get(task_id).feedback if self.hang_store.get(task_id) else ())
                if f.kind is FeedbackKind.RESULT)
            status = PLAN_CANCELLED if row.get("cancelled") else PLAN_BLOCKED if failed else PLAN_DONE if done else "interrupted"
            return {"plan_id": plan_id, "mode": row["plan"]["mode"], "need": row["need"],
                    "status": status,
                    "note": "索引已恢复；未自动重放工具", "steps": steps,
                    "agent_task_id": agent_id, "agent_status": agent.status if agent else ""}
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
        terminal = any(item.kind is FeedbackKind.RESULT for item in batch)
        has_progress = any(item.kind is FeedbackKind.PROGRESS for item in batch)
        summary_limit = (
            PROGRESS_NOTE_MAX_CHARS
            if has_progress and not terminal
            else None
        )
        skill = self.wrap_skill
        if skill is None:
            return
        try:
            result = skill.wrap_hang(hang, batch)
        except Exception:
            logger.exception("tool wrap skill failed")
            result = None
        if result is None:
            terminal_result = next(
                (
                    item.result
                    for item in reversed(batch)
                    if item.kind is FeedbackKind.RESULT and item.result is not None
                ),
                None,
            )
            if terminal_result is None:
                return
            text = _terminal_fallback_summary(terminal_result)
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
                summary = _terminal_fallback_summary(engine_result)
        self.hang_store.set_wrap(
            task_id,
            visible=bool(result.visible) and bool(summary),
            summary=summary,
            wrap_meta={"model_tag": getattr(skill, "model_tag", "")},
            terminal=terminal,
            summary_limit=summary_limit,
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
            if getattr(hang, "plan_mode", "") == "agent_loop":
                base = f"{base}；多步计划整体执行（{total}个建议步骤，未逐步确认）"
            elif total > 1:
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
        open_ttl_s: float = OPEN_IDLE_TTL_S,
        open_max_lifetime_s: float = OPEN_MAX_LIFETIME_S,
    ) -> tuple[int, int]:
        """清理卡死账本：超时仍 received 的策划、过久无进度或总时长超限的 open 记挂。

        open 的「无进度」按 ``updated_at`` 算；回写回应与送达标记不更新它，
        只有引擎反馈、阶段、包装会更新。总时长从建档算起。
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
                # 有终态正文时保留完整内容；旧记录只有短摘要时，才从有效进度恢复。
                text = _terminal_fallback_summary(last_result)
                if not last_result.result:
                    from jshi.tool.pi_engine import _shell_output_is_exploration

                    parts: list[str] = []
                    for item in hang.feedback:
                        if item.kind is not FeedbackKind.PROGRESS or item.progress is None:
                            continue
                        partial = (item.progress.partial or "").strip()
                        if partial and not _shell_output_is_exploration(partial) and partial not in parts:
                            parts.append(partial)
                    if parts:
                        text = "\n".join(parts)
                    elif _shell_output_is_exploration(text):
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
            idle = (stamp - hang.updated_at).total_seconds()
            lifetime = (stamp - hang.created_at).total_seconds()
            interrupted = bool(self.work_index.for_task(hang.task_id)) and hang.task_id not in self._session_tasks
            if not interrupted and idle <= open_ttl_s and lifetime <= open_max_lifetime_s:
                continue
            if interrupted and hang.kind == "propose":
                continue  # an approval proposal has not run; do not call it interrupted execution
            if interrupted:
                self.hang_store.patch_meta(hang.task_id, {**dict(hang.meta), "cancel_reason": "进程中断"})
            if self.cancel(hang.task_id):
                cancelled_open += 1
        return failed_planning, cancelled_open

    def list_tool_related_entries(
        self,
        subject_id: str,
        object_id: str,
        *,
        query: str = "",
        now: datetime | None = None,
        ttl_s: float = HOT_STATE_TTL_S,
        reap: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        """组装【工具相关】条目（程序侧结构，再交给 format_tool_related）。

        含：策划中（intake received、205 在跑）、使用中记挂、近时终态、
        近时取消、策划失败。已被 ``invalidate`` 的不出现。
        相同需求和参数的重复执行归并；未消化结果保留到 05 标明已消化。
        旧结果只在当前问题再次相关时装入，账本本身不丢。
        默认只读；``reap=True`` 时先 ``reap_stale``（主流程在组装前单独调用）。
        """
        stamp = now or utc_now()
        if reap:
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
            if age > ttl_s and record.delivered_at is not None:
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
                    "updated_at": record.updated_at,
                    "delivered_at": record.delivered_at,
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
                if age > ttl_s and hang.delivered_at is not None:
                    continue
                final = "已取消，不再执行"
                if "进程中断" in str((hang.meta or {}).get("cancel_reason") or ""):
                    final = "上次进程中断，已有索引保留；需要重新发起缺失步骤，未自动重放工具"
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
                        "updated_at": hang.updated_at,
                        "delivered_at": hang.delivered_at,
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
                    "updated_at": hang.updated_at,
                    "delivered_at": hang.delivered_at,
                    "params": hang.params,
                    "final_result": (hang.summary or "").strip(),
                    "end_reason": next((f.result.error or f.result.ideal_note
                        for f in reversed(hang.feedback)
                        if f.kind is FeedbackKind.RESULT and f.result is not None), ""),
                }
            )

        for entry in entries:
            works = self.work_index.for_task(str(entry["id"]))
            entry["work_ids"] = [row["work_id"] for row in works]
            entry["work_open"] = any(not row["closed"] for row in works)
            entry["handling"] = ", ".join(
                str(row["handling"].get(entry["id"], {}).get("disposition") or "待处理")
                for row in works)
            entry["work_needs"] = [row["need"] for row in works if not row["closed"]]
            entry["pending_steps"] = [step["step_id"] for row in works if not row["closed"]
                and (row.get("plan") or {}).get("mode") != "agent_loop"
                for step in (row.get("plan") or {}).get("steps", ())
                if step["step_id"] not in row["tasks"] or
                (self.hang_store.get(row["tasks"][step["step_id"]]) is None or
                 self.hang_store.get(row["tasks"][step["step_id"]]).status == "open")]
            hang = self.hang_store.get(str(entry["id"]))
            if hang is not None:
                if hang.plan_mode == "agent_loop":
                    entry["tool_name"] = hang.command or "多步工具任务"
                    entry["plan_progress"] = {
                        "running": "整体执行中，尚未逐步确认",
                        "done": "整体执行完成",
                        "partial": "整体部分完成，具体步骤尚未最终确认",
                        "failed": "整体执行未成功，具体步骤尚未确认",
                    }[hang_hot_phase(hang)]
                entry["reused_from"] = hang.meta.get("reused_from", "")
                if entry["reused_from"]:
                    source = next((f for f in reversed(hang.feedback)
                                   if f.kind is FeedbackKind.RESULT), None)
                    if source:
                        entry["used_at"] = source.at
                if works and hang.status == "open" and hang.task_id not in self._session_tasks:
                    entry.update(section="recent", status="失败",
                                 final_result="上次进程中断，已有任务索引保留；本次未自动重放工具")
        return select_tool_related_entries(
            dedupe_recent_tool_entries(entries), query=query, now=stamp, ttl_s=ttl_s
        )

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
        query: str = "",
        now: datetime | None = None,
        ttl_s: float = HOT_STATE_TTL_S,
        reap: bool = False,
    ) -> str:
        """该对象【工具相关】全文；空则 ``\"\"``。"""
        return format_tool_related(
            self.list_tool_related_entries(
                subject_id, object_id, query=query, now=now, ttl_s=ttl_s, reap=reap
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
        self, subject_id: str, object_id: str, *, item_ids: Sequence[str] | None = None
    ) -> tuple[str, ...]:
        """本轮【工具相关】已交给 05：标记可见句已送入主流程，不写片场。"""
        delivered: list[str] = []
        if item_ids is None:
            ids = tuple(item.id for item in self.pending_for_scene(subject_id, object_id)[:SCENE_BLOCK_CAP])
        else:
            ids = tuple(dict.fromkeys(item_ids))
        for item_id in ids:
            intake = self.intake_store.get(item_id)
            hang = self.hang_store.get(item_id)
            if intake is not None and intake.status == "failed":
                if intake.subject_id != subject_id or intake.object_id != object_id:
                    continue
                if intake.delivered_at is not None:
                    continue
                marked = self.intake_store.set_delivered(item_id, block="")
            elif hang is not None:
                if hang.subject_id != subject_id or hang.object_id != object_id:
                    continue
                if hang.delivered_at is not None and not any(
                    not row["closed"] for row in self.work_index.for_task(item_id)
                ):
                    continue
                if not (hang.summary or "").strip():
                    continue
                marked = self.hang_store.set_delivered(item_id, block="")
            else:
                continue
            if marked is None:
                continue
            delivered.append(item_id)
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
