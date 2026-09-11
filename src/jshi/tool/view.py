"""工具过程只读视图：给对话 /tool 与 ``jshi tool-log`` 用。不调引擎、不改账本。"""

from __future__ import annotations

import json

from .contract import FeedbackKind, ToolFeedback
from .hang import HangRecord, is_stage_fact
from .intake import IntakeRecord
from .service import ToolService, VisibleToolItem

CLIP = 240
RAW_CLIP = 4000


def _clip(text: str, limit: int = CLIP) -> str:
    value = (text or "").replace("\r\n", "\n").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def _at(record) -> str:
    stamp = getattr(record, "updated_at", None)
    if stamp is None:
        return ""
    iso = getattr(stamp, "isoformat", lambda: str(stamp))()
    return str(iso)


def _format_feedback(item: ToolFeedback, *, raw: bool) -> str:
    limit = RAW_CLIP if raw else CLIP
    kind = item.kind.value
    if item.kind is FeedbackKind.ESTIMATE and item.estimate is not None:
        est = item.estimate
        return (
            f"- estimate  benefit={_clip(est.benefit, limit)}  "
            f"downside={_clip(est.downside, limit)}  "
            f"time_est_ms={est.time_est_ms}  cost_est={est.cost_est}  "
            f"need_confirm={est.need_confirm}"
        )
    if item.kind is FeedbackKind.PROGRESS and item.progress is not None:
        prog = item.progress
        flag = "  [阶段事实→包装]" if is_stage_fact(item) else "  [心跳，不包装]"
        return (
            f"- progress{flag}  stage={prog.stage}  step={prog.step}  "
            f"note={_clip(prog.note, limit)}  partial={_clip(prog.partial, limit)}"
        )
    if item.kind is FeedbackKind.RESULT and item.result is not None:
        res = item.result
        payload = ""
        if raw and res.result:
            payload = "  result=" + _clip(
                json.dumps(dict(res.result), ensure_ascii=False), limit
            )
        elif res.result:
            tool = res.result.get("tool")
            if tool:
                payload = f"  tool={tool}"
        return (
            f"- result  status={res.status.value}  ideal={res.ideal}  "
            f"summary={_clip(res.summary, limit)}  error={_clip(res.error, limit)}"
            f"{payload}"
        )
    return f"- {kind}"


def _pick_intake(
    intakes: tuple[IntakeRecord, ...],
    hangs: tuple[HangRecord, ...],
    item_id: str,
) -> tuple[IntakeRecord | None, HangRecord | None]:
    if item_id:
        for record in intakes:
            if record.intake_id == item_id:
                hang = None
                if record.task_id:
                    hang = next((item for item in hangs if item.task_id == record.task_id), None)
                return record, hang
        for hang in hangs:
            if hang.task_id == item_id or hang.request_id == item_id:
                twin = next((item for item in intakes if item.task_id == hang.task_id), None)
                return twin, hang
        return None, None
    intake = intakes[0] if intakes else None
    hang = None
    if intake is not None and intake.task_id:
        hang = next((item for item in hangs if item.task_id == intake.task_id), None)
    if hang is None and hangs:
        hang = hangs[0]
    return intake, hang


def _format_list(
    intakes: tuple[IntakeRecord, ...],
    hangs: tuple[HangRecord, ...],
    visible: tuple[VisibleToolItem, ...],
) -> str:
    lines = ["【交接一览】"]
    if not intakes:
        lines.append("（无）")
    for record in intakes[:12]:
        lines.append(
            f"- {record.intake_id[:8]}  {record.status}  "
            f"need={_clip(record.need, 40)}  task={record.task_id[:8] or '—'}"
        )
    lines.append("【记挂一览】")
    if not hangs:
        lines.append("（无）")
    for hang in hangs[:12]:
        flag = "可见" if hang.visible else "隐藏"
        lines.append(
            f"- {hang.task_id[:8]}  {hang.status}/{flag}  "
            f"tpl={hang.template or '—'}  summary={_clip(hang.summary, 40)}"
        )
    lines.append("【03 将装 list_visible】")
    if not visible:
        lines.append("（无）")
    for item in visible:
        lines.append(f"- {item.kind} {item.id[:8]}  {_clip(item.summary, 60)}")
    return "\n".join(lines)


def format_tool_process(
    service: ToolService,
    *,
    subject_id: str,
    object_id: str = "",
    last_use_tool: bool | None = None,
    last_need: str = "",
    last_verbal: str = "",
    item_id: str = "",
    listing: bool = False,
    raw: bool = False,
) -> str:
    """拼出 05 → 交接 → 引擎反馈 → 记挂/包装 → 03 可见句。"""
    if object_id:
        intakes = service.intake_store.list_for(subject_id, object_id)
        hangs = service.hang_store.list_for(subject_id, object_id)
        visible = service.list_visible(subject_id, object_id)
    else:
        intakes = service.intake_store.list_for_subject(subject_id)
        hangs = service.hang_store.list_for_subject(subject_id)
        visible = ()
        seen: dict[str, list[VisibleToolItem]] = {}
        for record in intakes:
            seen.setdefault(record.object_id, [])
        for object_key in seen:
            visible = visible + service.list_visible(subject_id, object_key)

    if listing:
        return _format_list(intakes, hangs, visible)

    intake, hang = _pick_intake(intakes, hangs, item_id.strip())
    if item_id.strip() and intake is None and hang is None:
        return f"找不到 {item_id}。可用 /tool list 看 id。"

    engine = ""
    runner = getattr(service, "runner", None)
    module = getattr(runner, "module", None) if runner is not None else None
    if module is not None:
        engine = str(getattr(module, "engine_name", "") or "")

    lines: list[str] = []
    lines.append("【05 触发】")
    if last_use_tool is None:
        lines.append("本会话还没有上一轮认知，或未保存指示。")
    elif last_use_tool:
        lines.append("use_tool=true")
        lines.append(f"need={last_need or '（空）'}")
        lines.append(f"口头={last_verbal or '（无）'}")
    else:
        lines.append("use_tool 未标（本轮 05 不用工具）")
        if last_verbal:
            lines.append(f"口头={last_verbal}")

    lines.append("【200 交接】")
    if intake is None:
        lines.append("（还没有 intake）")
    else:
        ref = json.dumps(dict(intake.field_ref), ensure_ascii=False) if intake.field_ref else "{}"
        lines.append(f"intake_id={intake.intake_id}")
        lines.append(f"status={intake.status}  origin={intake.origin}  object={intake.object_id}")
        lines.append(f"activity={intake.activity_id or '—'}  updated={_at(intake)}")
        lines.append(f"need={intake.need or '（空）'}")
        lines.append(f"verbal={intake.verbal or '（无）'}")
        lines.append(f"field_ref={ref}")
        if intake.plan_error:
            lines.append(f"plan_error={intake.plan_error}")
        tag = (intake.meta or {}).get("model_tag") or ""
        if tag:
            lines.append(f"plan_model_tag={tag}")
        if intake.task_id:
            lines.append(f"task_id={intake.task_id}  request_id={intake.request_id}")

    lines.append("【记挂 210】")
    if hang is None:
        lines.append("（无记挂：尚未发出请求，或策划失败）")
    else:
        lines.append(f"task_id={hang.task_id}  request_id={hang.request_id}")
        lines.append(
            f"status={hang.status}  visible={hang.visible}  template={hang.template or '—'}"
        )
        lines.append(f"need={hang.need}")
        if hang.note:
            lines.append(f"note={hang.note}")
        plan_tag = (hang.meta or {}).get("plan_model_tag") or ""
        if plan_tag:
            lines.append(f"plan_model_tag={plan_tag}")
        wrap_tag = (hang.wrap_meta or {}).get("model_tag") or (hang.wrap_meta or {}).get("source") or ""
        lines.append("【200 对记挂的处理（包装）】")
        lines.append(f"visible={hang.visible}  status={hang.status}")
        lines.append(f"summary={hang.summary or '（空，03 不装）'}")
        if wrap_tag:
            lines.append(f"wrap_meta={dict(hang.wrap_meta)}")
        if hang.status == "open" and hang.visible:
            lines.append("（进度已可见，尚未终态；notified 只在终态包装后）")

        lines.append("【引擎反馈】" + (f"  engine={engine}" if engine else ""))
        if not hang.feedback:
            lines.append("（尚无反馈）")
        else:
            for item in hang.feedback:
                lines.append(_format_feedback(item, raw=raw))

    lines.append("【03 将装】")
    if not visible:
        lines.append("（list_visible 为空，tool_input 不会出现）")
    else:
        for item in visible:
            mark = ""
            if hang is not None and item.id == hang.task_id:
                mark = "  ← 上面这本"
            elif intake is not None and item.id == intake.intake_id:
                mark = "  ← 上面这条失败句"
            lines.append(f"- {item.kind} {_clip(item.summary, 80)}{mark}")
    return "\n".join(lines)
