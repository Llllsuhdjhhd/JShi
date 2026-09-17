"""工具过程只读视图：给对话 /tool 与 ``jshi tool-log`` 用。不调引擎、不改账本。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .contract import FeedbackKind, ToolFeedback, ToolStatus
from .discovery import ToolIndex
from .hang import HangRecord, is_stage_fact
from .intake import IntakeRecord
from .service import HOT_STATE_TTL_S, ToolService, VisibleToolItem

CLIP = 240
RAW_CLIP = 4000
SCENE_CLIP = 8000
# list 印 8 位；短于 4 位不配，避免 /tool ins 这类碎片误中。
_MIN_ID_PREFIX = 4


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


def _id_hit(stored: str, query: str) -> bool:
    left = (stored or "").strip().lower()
    right = (query or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    return len(right) >= _MIN_ID_PREFIX and left.startswith(right)


def _hang_for(
    record: IntakeRecord,
    hangs: tuple[HangRecord, ...],
) -> HangRecord | None:
    if not record.task_id:
        return None
    return next((item for item in hangs if item.task_id == record.task_id), None)


def _pick_intake(
    intakes: tuple[IntakeRecord, ...],
    hangs: tuple[HangRecord, ...],
    item_id: str,
) -> tuple[IntakeRecord | None, HangRecord | None, str]:
    query = (item_id or "").strip()
    if not query:
        intake = intakes[0] if intakes else None
        hang = None
        if intake is not None:
            hang = _hang_for(intake, hangs)
        if hang is None and hangs:
            hang = hangs[0]
        return intake, hang, ""

    picks: list[tuple[IntakeRecord | None, HangRecord | None]] = []
    seen: set[str] = set()

    def add(intake: IntakeRecord | None, hang: HangRecord | None) -> None:
        key = (
            (intake.intake_id if intake is not None else ""),
            (hang.task_id if hang is not None else ""),
        )
        if key in seen:
            return
        seen.add(key)
        picks.append((intake, hang))

    for record in intakes:
        ids = (record.intake_id, record.task_id, record.request_id)
        if any(_id_hit(value, query) for value in ids):
            add(record, _hang_for(record, hangs))
    for hang in hangs:
        ids = (hang.task_id, hang.request_id)
        if any(_id_hit(value, query) for value in ids):
            twin = next((item for item in intakes if item.task_id == hang.task_id), None)
            add(twin, hang)

    if not picks:
        return None, None, f"找不到 {item_id}。可用 /tool list 看 id。"
    if len(picks) > 1:
        labels = []
        for intake, hang in picks:
            if intake is not None:
                labels.append(intake.intake_id[:8])
            elif hang is not None:
                labels.append(hang.task_id[:8])
        shown = "、".join(labels)
        return None, None, f"短号 {item_id} 不唯一（{shown}）。请写更长一点。"
    intake, hang = picks[0]
    return intake, hang, ""


def _hang_status_label(hang: HangRecord) -> str:
    if hang.status == "open":
        return "使用中"
    if hang.status == "cancelled":
        return "已取消"
    if hang.status == "notified":
        last = next(
            (item for item in reversed(hang.feedback) if item.kind is FeedbackKind.RESULT),
            None,
        )
        if last is not None and last.result is not None:
            value = last.result.status
            if value in {
                ToolStatus.FAILED,
                ToolStatus.PARTIAL,
                ToolStatus.ABORTED,
                ToolStatus.REJECTED,
                ToolStatus.BUDGET_EXCEEDED,
            }:
                return "失败"
        return "已完成"
    return hang.status or "—"


def _hang_description(service: ToolService, hang: HangRecord) -> str:
    describe = getattr(service, "_tool_description_for", None)
    if callable(describe):
        text = describe(
            command=hang.command,
            kind=hang.kind,
            meta=hang.meta,
            hang=hang,
        )
        if text:
            return text
    return (hang.command or hang.template or hang.kind or "外部工具").strip()


def _recent_feedback_line(hang: HangRecord, *, raw: bool) -> str:
    if not hang.feedback:
        return "（尚无引擎反馈）"
    last = hang.feedback[-1]
    return _format_feedback(last, raw=raw).lstrip("- ").strip()


def _emit_now_hang(
    lines: list[str],
    hang: HangRecord,
    *,
    service: ToolService,
    raw: bool,
) -> None:
    when = _at(hang)
    step = ""
    if int(hang.plan_total or 0) > 1:
        index = hang.plan_index if hang.plan_index > 0 else "?"
        step = f"  计划第{index}/{hang.plan_total}步"
    lines.append(f"- {_hang_status_label(hang)}  {hang.task_id[:8]}{step}")
    lines.append(f"  需求：{hang.need or '（无）'}")
    lines.append(f"  工具：{hang.command or hang.template or '—'}")
    lines.append(f"  描述：{_clip(_hang_description(service, hang), CLIP)}")
    if hang.note and hang.note.strip() != (hang.need or "").strip():
        lines.append(f"  备注：{_clip(hang.note, CLIP)}")
    if hang.summary:
        lines.append(f"  包装：{_clip(hang.summary, CLIP)}")
    if hang.delivered_at is not None:
        lines.append("  交付：已进片场")
    elif hang.visible:
        lines.append("  交付：可见、未进片场")
    if when:
        lines.append(f"  更新：{when}")
    lines.append(f"  最近反馈：{_clip(_recent_feedback_line(hang, raw=raw), RAW_CLIP if raw else CLIP)}")


def format_tool_now(
    service: ToolService,
    *,
    subject_id: str,
    object_id: str = "",
    now: datetime | None = None,
    ttl_s: float = HOT_STATE_TTL_S,
    raw: bool = False,
) -> str:
    """当前对象：使用中的工具，以及近时刚完成/失败的工具。

    与【工具相关】同一口径：进行中一律列出；终态只保留 ``ttl_s`` 内。
    同一 need 拆成多本记挂会并排出现，不是矛盾——05 仍是一条 need，200 按步各建一本。
    """
    stamp = now or datetime.now(timezone.utc)
    intakes, hangs, _visible = _collections(
        service, subject_id=subject_id, object_id=object_id
    )
    in_use: list[HangRecord] = []
    recent: list[HangRecord] = []
    for hang in hangs:
        if hang.status == "open":
            in_use.append(hang)
            continue
        if hang.status in {"notified", "cancelled"}:
            age = (stamp - hang.updated_at).total_seconds()
            if age <= ttl_s:
                recent.append(hang)

    planning: list[IntakeRecord] = []
    for record in intakes:
        if record.status == "received" and not (record.task_id or "").strip():
            age = (stamp - record.updated_at).total_seconds()
            if age <= ttl_s:
                planning.append(record)

    lines = [
        "【当前工具】",
        "当前对象正在用的，以及近一小时刚完成或失败的。",
        "同一句话拆成多本记挂会并排列出（例如赛程一步、天气一步）；05 仍是一条 need。",
        "细账用 /tool <id>；本页只看正在办与刚办完的。",
    ]
    lines.append("")
    lines.append("# 使用中")
    if not planning and not in_use:
        lines.append("（无）")
    for record in planning:
        lines.append(f"- 策划中  {record.intake_id[:8]}")
        lines.append(f"  需求：{record.need or '（无）'}")
        lines.append("  描述：正在形成可执行步骤，尚无工具名")
        lines.append(f"  更新：{_at(record)}")
        lines.append("  最近反馈：（策划尚未发出记挂）")
    for hang in in_use:
        _emit_now_hang(lines, hang, service=service, raw=raw)

    lines.append("")
    lines.append("# 刚使用")
    if not recent:
        lines.append("（无）")
    for hang in recent:
        _emit_now_hang(lines, hang, service=service, raw=raw)
    return "\n".join(lines)


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
        state = "已交付" if hang.delivered_at is not None else "未交付"
        lines.append(
            f"- {hang.task_id[:8]}  {hang.status}/{flag}/{state}  "
            f"tpl={hang.template or '—'}  summary={_clip(hang.summary, 40)}"
        )
    lines.append("【当前可见（未交付）list_visible】")
    if not visible:
        lines.append("（无）")
    for item in visible:
        lines.append(f"- {item.kind} {item.id[:8]}  {_clip(item.summary, 60)}")
    return "\n".join(lines)


def _collections(
    service: ToolService,
    *,
    subject_id: str,
    object_id: str,
) -> tuple[tuple[IntakeRecord, ...], tuple[HangRecord, ...], tuple[VisibleToolItem, ...]]:
    if object_id:
        return (
            service.intake_store.list_for(subject_id, object_id),
            service.hang_store.list_for(subject_id, object_id),
            service.list_visible(subject_id, object_id),
        )
    intakes = service.intake_store.list_for_subject(subject_id)
    hangs = service.hang_store.list_for_subject(subject_id)
    visible: tuple[VisibleToolItem, ...] = ()
    seen: set[str] = set()
    for record in intakes:
        if record.object_id and record.object_id not in seen:
            seen.add(record.object_id)
            visible = visible + service.list_visible(subject_id, record.object_id)
    return intakes, hangs, visible


def _planner_materials(
    service: ToolService,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    planner = getattr(service, "planner", None)
    catalog = tuple(getattr(planner, "catalog", ()) or ())
    if not catalog:
        from .catalog import load_catalog

        catalog = load_catalog()
    tools: tuple[Mapping[str, Any], ...] = ()
    resolve = getattr(planner, "_resolve_tools", None)
    if callable(resolve):
        tools = tuple(resolve() or ())
    else:
        runner = getattr(service, "runner", None)
        module = getattr(runner, "module", None) if runner is not None else None
        engine = getattr(module, "engine", None) if module is not None else None
        if engine is not None:
            from .catalog import engine_tools

            tools = engine_tools(engine)
    return catalog, tools


def _format_named_items(items: Sequence[Mapping[str, Any]], *, raw: bool) -> list[str]:
    if not items:
        return ["（无）"]
    limit = RAW_CLIP if raw else 120
    lines: list[str] = []
    for item in items:
        name = str(item.get("name") or "").strip() or "—"
        note = (
            str(item.get("description") or "").strip()
            or str(item.get("intent_slot") or "").strip()
        )
        if note:
            lines.append(f"- {name}  {_clip(note, limit)}")
        else:
            lines.append(f"- {name}")
    return lines


def format_tool_plan_view(
    service: ToolService,
    *,
    subject_id: str,
    object_id: str = "",
    item_id: str = "",
    scene_loader: Callable[[IntakeRecord], str] | None = None,
    zone_kind: str = "",
    raw: bool = False,
) -> str:
    """拼出 205 策划任务：交接字段 + 现读现场 + 目录。不调模型。"""
    intakes, hangs, _visible = _collections(
        service, subject_id=subject_id, object_id=object_id
    )
    intake, _hang, error = _pick_intake(intakes, hangs, (item_id or "").strip())
    if (item_id or "").strip() and error:
        return error

    live = True
    if intake is None:
        live = False
        intake = IntakeRecord(
            intake_id="",
            subject_id=subject_id,
            object_id=object_id,
            field_ref={"zone_kind": (zone_kind or "wood")},
        )
    scene = ""
    if scene_loader is not None:
        try:
            scene = (scene_loader(intake) or "").strip()
        except Exception:
            scene = ""
    catalog, tools = _planner_materials(service)
    index = ToolIndex.from_maps(tools)
    scene_limit = RAW_CLIP * 4 if raw else SCENE_CLIP
    ref = json.dumps(dict(intake.field_ref), ensure_ascii=False) if intake.field_ref else "{}"
    lines = [
        "【205 策划材料】",
        "不调模型。scene 是现在读的现场，不是交接账本里的快照。",
    ]
    if not live:
        lines.append("（还没有 intake；下面现场按当前写法现读）")
    else:
        lines.append(f"intake_id={intake.intake_id}")
        lines.append(f"status={intake.status}  object={intake.object_id}")
    lines.append(f"need={intake.need or '（空）'}")
    lines.append(f"verbal={intake.verbal or '（无）'}")
    lines.append(f"object_id={intake.object_id or '—'}")
    lines.append(f"field_ref={ref}")
    lines.append("【scene · 现读】")
    lines.append(_clip(scene, scene_limit) if scene else "（空）")
    lines.append("【catalog】")
    lines.extend(_format_named_items(catalog, raw=raw))
    lines.append("【engine_tools】")
    lines.extend(index.format_view(raw=raw))
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
    intakes, hangs, visible = _collections(
        service, subject_id=subject_id, object_id=object_id
    )

    if listing:
        return _format_list(intakes, hangs, visible)

    intake, hang, error = _pick_intake(intakes, hangs, item_id.strip())
    if error:
        return error

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
            f"status={hang.status}  visible={hang.visible}  kind={hang.kind or '—'}  "
            f"command={hang.command or '—'}  template={hang.template or '—'}"
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

        lines.append("【交付片场】")
        if hang.delivered_at is None:
            lines.append("（尚未交付：仍走 tool_input）")
        else:
            lines.append(f"delivered_at={hang.delivered_at.isoformat()}")
            lines.append(f"块={hang.delivered_block or '（空）'}")
            lines.append("（已交付：不再走 tool_input；随片场压缩自然过期，账本仍保留）")

        lines.append("【05 回写】")
        if not hang.responses:
            lines.append("（还没有回应回写）")
        else:
            for item in hang.responses:
                lines.append(
                    f"- mode={item.get('mode') or '—'}  "
                    f"reply={_clip(str(item.get('reply') or ''), 80) or '（未开口）'}  "
                    f"action={_clip(str(item.get('action') or ''), 40) or '—'}"
                )

        lines.append("【引擎反馈】" + (f"  engine={engine}" if engine else ""))
        if not hang.feedback:
            lines.append("（尚无反馈）")
        else:
            for item in hang.feedback:
                lines.append(_format_feedback(item, raw=raw))

    lines.append("【当前可见（未交付）】")
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


def format_turn_view(
    service: ToolService,
    history: Sequence[Mapping[str, Any]],
    *,
    subject_id: str,
    object_id: str = "",
) -> str:
    """CLI 版「主流程这一拍给了 200 什么」：读历史里的 05 指示 + 算 200 的当前可见。

    ``history`` 是 ``(event_type, content)`` 序列（按时间正序）。只读，不调模型、不跑引擎。
    """
    lines: list[str] = ["【05 触发（主流程落库）】"]
    marked = [item for item in history if str(item[0]) == "activity_response_marked"]
    if not marked:
        lines.append(
            "（历史里没有工具指示：这个 data-dir 还没跑过「标了要用」的一拍，"
            "或那一拍不是用这条路径落库的。）"
        )
    else:
        _event_type, content = marked[-1]
        lines.append(f"use_tool={'true' if content.get('use_tool') else 'false'}")
        lines.append(f"need={_clip(str(content.get('need') or ''), 240) or '（空）'}")
        lines.append(f"mode={content.get('mode') or '—'}")
        lines.append(f"reply={_clip(str(content.get('reply') or ''), 200) or '（未开口）'}")

    lines.append("【200 侧（按当前账本算）】")
    if not object_id:
        lines.append("（未定位对象；加 --speaker / --object-id 才能看该对象的可见集）")
    else:
        pending = service.pending_for_scene(subject_id, object_id)
        lines.append(f"未交付（会进 tool_input）：{len(pending)} 条")
        for item in pending:
            lines.append(f"- {item.kind} {item.id[:8]}  {_clip(item.summary, 80)}")
        hangs = service.hang_store.list_for(subject_id, object_id)
        delivered = [h for h in hangs if h.delivered_at is not None]
        lines.append(f"已交付进片场：{len(delivered)} 条（不再走 tool_input）")
        for record in delivered:
            at = _at(record)
            lines.append(f"- {record.task_id[:8]}  {at}  {_clip(record.delivered_block, 80)}")
        responded = [h for h in hangs if h.responses]
        lines.append(f"已回写 05 回应：{len(responded)} 条")
        for record in responded:
            last = record.responses[-1]
            lines.append(
                f"- {record.task_id[:8]}  mode={last.get('mode') or '—'}  "
                f"reply={_clip(str(last.get('reply') or ''), 60) or '（未开口）'}"
            )
    return "\n".join(lines)
