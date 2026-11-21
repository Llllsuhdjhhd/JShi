"""现场（设计 §4.1）：自省看见什么。

只读，三份材料拼成现场：

```
① 当时的上下文   ┐
② 前后现场       ├ 账本时间窗：按时间取（不按段数），取 [中心 − Δ, 中心 + Δ] 内的经历段
③ 09 回溯召回    ┘ 拿条目文本去 09 召回，补背景与类似经历
```

- "当时"= 被考察条目发生的时间段；``entry_at_also`` 给第二个中心（如"迁移发生时"）；
- 段取 ``accepted`` / ``consumed``（已收尾），不取 ``archived``；
- 材料不足不硬编：返回 ``None``，由调用方记 ``dropped(no_material)``；
- 不改写账本、不写活跃区、不改个人世界。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta

from jshi.core.params import (
    introspection_recall_limit,
    introspection_scene_max_segments,
    introspection_scene_window_seconds,
    reflection_param,
)
from jshi.core.scene_text import observation_text
from jshi.experienceledger.port import SegmentStatus, segment_view_text
from jshi.memory.port import RecalledFragment

from .port import IntrospectionRequest, IntrospectionScene, IntrospectionTrigger

_USABLE_STATUSES = (SegmentStatus.ACCEPTED, SegmentStatus.CONSUMED)


def segment_text(segment: object) -> str:
    """取一段经历的可读正文：原文优先，其次内部状态摘要。"""
    text = segment_view_text(segment).strip()
    if text:
        return text
    state = getattr(segment, "state_delta", None)
    if state:
        return _brief(state)
    plan = getattr(segment, "response_plan", None)
    if plan:
        return _brief(plan)
    return ""


def build_scene(
    request: IntrospectionRequest,
    *,
    ledger: object,
    memory: object,
    window_seconds: int | None = None,
    max_segments: int | None = None,
    recall_limit: int | None = None,
    for_question: bool = False,
) -> IntrospectionScene | None:
    """按时间窗取段 + 召回一次，拼成现场；没材料返回 ``None``。"""
    if window_seconds is None:
        window_seconds = introspection_scene_window_seconds()
    if max_segments is None:
        max_segments = introspection_scene_max_segments()
    if recall_limit is None:
        recall_limit = introspection_recall_limit()
    centers = request.centers
    delta = timedelta(seconds=max(1, int(window_seconds)))
    anchors = tuple(request.evidence.get("segment_ids", ())) if for_question else ()
    recall_question = for_question and request.origin == "recall_reprocess"
    if recall_question and not anchors:
        all_segments = ()
    elif anchors and hasattr(ledger, "experiences_by_ids"):
        all_segments = ledger.experiences_by_ids(request.subject_id, anchors[:max_segments])
    elif for_question and hasattr(ledger, "read_experience_page"):
        rows = {}
        for center in dict.fromkeys(centers):
            page = ledger.read_experience_page(request.subject_id, start=center - delta,
                end=center + delta + timedelta(microseconds=1), limit=max_segments,
                newest_first=True, object_id=request.object_id)
            rows.update((row.segment_id, row) for row in page)
        all_segments = rows.values()
    else:
        try:
            all_segments = ledger.list_experiences(request.subject_id)
        except Exception:
            if for_question:
                raise
            all_segments = ()

    picked = [
        segment
        for segment in all_segments
        if getattr(segment, "status", None) in _USABLE_STATUSES
        and getattr(segment, "subject_id", request.subject_id) == request.subject_id
        and (segment.segment_id in anchors if anchors else _within(segment, centers, window_seconds))
    ]
    picked.sort(key=lambda item: getattr(item, "sequence", 0))
    if len(picked) > max_segments:
        picked = _nearest(picked, centers, max_segments)

    query = (request.entry_text or "").strip()
    if not query and picked:
        query = segment_text(picked[0])
    recalled: tuple[object, ...] = ()
    needs_examples = (not anchors and not request.evidence.get("tool_facts")
                      or request.evidence.get("method") == "recall")
    if recall_question:
        recalled = _observation_fragments(request.evidence.get("recall_events", ()))
    elif query and memory is not None and (not for_question or needs_examples):
        try:
            with _internal_recall(memory):
                recalled = tuple(memory.recall(request.subject_id, query, limit=recall_limit,
                                               object_id=request.object_id, level=1) or ())
        except Exception:
            if for_question:
                raise
            recalled = ()

    if not picked and not recalled:
        return None

    window = (min(centers) - delta, max(centers) + delta)
    source_ids = tuple(
        dict.fromkeys(
            [
                *(getattr(segment, "segment_id", "") for segment in picked),
                *(getattr(fragment, "event_id", "") for fragment in recalled),
            ]
        )
    )
    source_ids = tuple(item for item in source_ids if item)
    segments = tuple(picked)
    return IntrospectionScene(
        request=request,
        window=window,
        segments=segments,
        recalled=recalled,
        text="" if for_question else render_scene(request, segments, recalled, window),
        source_ids=source_ids,
    )


@contextmanager
def _internal_recall(memory):
    local = getattr(memory, "_reflection_local", None)
    if local is None:
        yield
        return
    previous = getattr(local, "internal", False)
    local.internal = True
    try:
        yield
    finally:
        local.internal = previous


def _observation_fragments(events):
    fragments = []
    seen = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        ident = event.get("event_id")
        content = event.get("content") or event.get("text")
        if not isinstance(ident, str) or not ident or ident in seen or not isinstance(content, str) or not content.strip():
            continue
        if ident.startswith("insight:") or event.get("kind") == "reflection_insight" or event.get("validity", "active") != "active":
            continue
        seen.add(ident)
        stamp = event.get("occurred_at")
        try:
            when = datetime.fromisoformat(str(stamp)) if stamp else None
        except ValueError:
            when = None
        people = tuple(event.get("interlocutor_object_ids") or ())
        if not people and event.get("query_object_role") == "interlocutor" and event.get("object_id"):
            people = (event["object_id"],)
        fragments.append(RecalledFragment(ident, "recalled_event", content, content=content,
                                          kind=event.get("kind", "fact"), occurred_at=when,
                                          interlocutor_object_ids=people))
    return tuple(fragments)


def _tool_return_excerpt(result):
    """Surface bounded business values. Keep raw diagnostics, paths and identifiers in the record."""
    if not isinstance(result, dict):
        return ""
    parts = []
    for key in ("content", "summary", "text", "excerpt", "answer", "value", "unit", "temperature", "count",
                "duration", "passed", "created", "echo", "结果", "数值", "单位"):
        value = result.get(key)
        if isinstance(value, (bool, int, float)) or isinstance(value, str) and value.strip():
            body = observation_text(str(value)).strip()
            if not body.startswith(("{", "[", "Traceback ")) and "\n" not in body:
                parts.append(f"{key}={body[:180]}")
    return "，".join(parts)[:400]


def freeze_question_materials(request, ledger, memory, repository=None):
    """Snapshot the exact user text and its private citation map, before the call."""
    scene = build_scene(request, ledger=ledger, memory=memory, for_question=True)
    segments = scene.segments if scene else ()
    memories = scene.recalled if scene else ()
    actors = {}
    participants_by_ref = {}
    refs = {}
    limit = int(reflection_param("REFLECTION_CONTEXT_CHARS"))
    lines = [f"【待思考的问题】{request.entry_text[:limit // 6]}"]
    reason = request.evidence.get("reason")
    if reason:
        lines.append("选题缘由：" + str(reason)[:limit // 8])
    for key, label in (("previous_open_question", "上次留下的疑问"), ("previous_waiting_for", "上次等待的条件")):
        previous = request.evidence.get(key)
        if isinstance(previous, str) and previous.strip():
            lines.append(label + "（仍待核对）：" + previous.strip()[:limit // 12])
    if request.object_id:
        actors[request.object_id] = "P1"
        lines.append("问题涉及的人：P1")
    lines.append("题目线索时间：" + _fmt(request.entry_at))
    if "baseline_status" in request.evidence:
        count = request.evidence.get("baseline_sample_count", 0)
        lines.append(f"发现时的前七天参照：同来源取样 {count} 段，不能据此推及一周全部活动。"
                     if count else "发现时的前七天参照：没有可对照的样本。")
    lines.append("【可核对的现场】")
    remaining = limit - len("\n".join(lines)) - 140
    counts = {"E": 0, "M": 0, "T": 0}
    partial = []
    tool_facts = () if request.origin == "recall_reprocess" else request.evidence.get("tool_facts", ())
    cognition = None
    revision = request.trigger == IntrospectionTrigger.EPISTEMIC_REVISED and request.entry_ref.startswith("cognition:")
    if revision and repository is not None:
        try:
            cognition = repository.get_cognitive_content(request.entry_ref.removeprefix("cognition:"))
        except KeyError:
            pass
    source_count = len(segments) + len(memories) + len(tool_facts) + int(revision)
    excerpt_limit = max(100, remaining // max(1, source_count))

    def add(kind, ident, body, label, people=()):
        nonlocal remaining
        if not ident or not isinstance(body, str) or not body.strip():
            return
        code = kind + str(counts[kind] + 1)
        participants = tuple(dict.fromkeys(p for p in people if p))
        codes = dict(actors)
        for person in participants:
            codes.setdefault(person, f"P{len(codes) + 1}")
        attribution = "、".join(codes[p] for p in participants)
        heading = f"[{code}] {label} {attribution}："
        available = min(excerpt_limit, remaining - len(heading) - 1)
        if available < min(25, len(body)):
            return
        if len(body) > available:
            body = body[:available - 5] + "…（节选）"
            partial.append(code)
        remaining -= len(heading) + len(body) + 1
        counts[kind] += 1
        actors.update(codes)
        participants_by_ref[code] = list(participants)
        refs[code] = [ident, body, participants[0] if len(participants) == 1 else None]
        lines.append(heading + body)

    if cognition is not None and cognition.subject_id == request.subject_id:
        add("E", request.entry_ref, cognition.content, "此前形成的认识 " + _fmt(cognition.created_at))
    for item in segments:
        body = observation_text(segment_view_text(item))
        if not body:
            state = item.state_delta or {}
            body = next((state[key] for key in ("summary", "reason", "description") if isinstance(state.get(key), str)), "")
        add("E", item.segment_id, body, _fmt(item.occurred_at) + " " + item.output_kind.value,
            (item.actor_object_id,) if item.actor_object_id else ())
    for item in memories:
        if item.kind == "reflection_insight" or item.event_id.startswith("insight:") or item.validity != "active":
            continue
        people = item.interlocutor_object_ids or (
            (item.object_id,) if item.query_object_role == "interlocutor" and item.object_id else ())
        add("M", item.event_id, observation_text(item.content or item.text), "原事件 " + _fmt(item.occurred_at), people)
    for fact in tool_facts:
        fields = [f"{name}：{fact[key]}" for key, name in (
            ("need", "原要求"), ("status", "登记状态"), ("summary", "回报摘要"), ("ideal_note", "与预期的差异"))
            if fact.get(key)]
        if fact.get("error") and fact["error"] != fact.get("summary"):
            fields.append("错误：" + str(fact["error"]))
        if not fact.get("summary") and not fact.get("error"):
            excerpt = _tool_return_excerpt(fact.get("actual_result"))
            fields.append("实际返回节选：" + excerpt if excerpt else "没有呈现可核对的返回值。")
        for key, label in (("time_ms", "工具耗时（毫秒）"), ("cost", "记录的成本")):
            if isinstance(fact.get(key), (float, int)) and not isinstance(fact[key], bool):
                fields.append(f"{label}：{fact[key]}")
        try:
            when = datetime.fromisoformat(str(fact["at"]))
        except (ValueError, KeyError):
            when = None
        add("T", fact.get("source_id"), "；".join(fields), "工具回报 " + _fmt(when))
    presented = {v[0] for v in refs.values()}
    if request.origin == "recall_reprocess":
        wanted = [*request.evidence.get("segment_ids", ()), *(item.event_id for item in memories)]
    else:
        wanted = [*request.evidence.get("segment_ids", ()),
                  *(fact["source_id"] for fact in tool_facts if fact.get("source_id"))]
        if revision:
            wanted.append(request.entry_ref)
    missing = [ident for ident in wanted if ident not in presented]
    if missing:
        lines.append(f"有 {len(missing)} 条指定材料未呈现。")
    if not refs:
        lines.append("（本次没有可核对的原件）")
    return {"text": "\n".join(lines), "references": refs, "actors": actors, "missing": missing, "partial": partial,
            "participants": participants_by_ref, "counts": counts,
            "subject": request.subject_id, "request_id": request.request_id}


def person_codes(request, segments, recalled):
    people = [request.object_id, *(getattr(s, 'actor_object_id', None) for s in segments)]
    for item in recalled:
        people.extend(getattr(item, 'interlocutor_object_ids', ()) or ())
        people.append(getattr(item, 'object_id', None))
    return {person: f'P{index}' for index, person in enumerate(dict.fromkeys(p for p in people if p), 1)}


def render_scene(
    request: IntrospectionRequest,
    segments: tuple[object, ...],
    recalled: tuple[object, ...],
    window: tuple[datetime, datetime],
) -> str:
    """把现场写成给模型看的正文（六问的输入）。"""
    actors = person_codes(request, segments, recalled)
    topic = request.entry_text
    for index, segment in enumerate(segments, 1):
        if request.entry_ref == f"segment:{getattr(segment, 'segment_id', '')}" and topic == segment_text(segment):
            topic = f'E{index}'
            break
    lines = [
        f"【现场｜来源 {request.trigger.value}】",
        f"对象：{actors.get(request.object_id, '主体自己')}",
        f"所审视的事：{topic}",
        f"当时：{_fmt(window[0])} ~ {_fmt(window[1])}",
        "",
        f"— 当时的上下文与前后现场（{len(segments)} 段，按时间）—",
    ]
    if segments:
        for index, segment in enumerate(segments, start=1):
            kind = getattr(getattr(segment, "output_kind", None), "value", "?")
            actor = actors.get(getattr(segment, "actor_object_id", None)) or getattr(
                getattr(segment, 'actor_kind', None), 'value', '系统')
            lines.append(
                f"[E{index}] {_fmt(getattr(segment, 'occurred_at', None))} "
                f"{kind}/{actor}：{segment_text(segment)}"
            )
    else:
        lines.append("（这一段没有可用经历）")

    lines.append("")
    lines.append(f"— 回溯（09 召回 {len(recalled)} 条）—")
    if recalled:
        for index, fragment in enumerate(recalled, start=1):
            body = str(
                getattr(fragment, "content", "")
                or getattr(fragment, "text", "")
                or ""
            )
            people = getattr(fragment, 'interlocutor_object_ids', ()) or ()
            labels = '、'.join(actors[p] for p in people if p in actors)
            lines.append(f"[M{index}] {_fmt(getattr(fragment, 'occurred_at', None))} {labels} {body}".strip())
    else:
        lines.append("（没有召回）")
    return "\n".join(lines)


def _within(segment: object, centers: tuple[datetime, ...], window: int) -> bool:
    occurred = getattr(segment, "occurred_at", None)
    if occurred is None:
        return False
    return any(abs((occurred - center).total_seconds()) <= window for center in centers)


def _nearest(
    segments: list[object], centers: tuple[datetime, ...], limit: int
) -> list[object]:
    """段太多时留离时间中心最近的，再按时间还原。"""

    def distance(segment: object) -> float:
        occurred = getattr(segment, "occurred_at", None)
        if occurred is None:
            return float("inf")
        return min(abs((occurred - center).total_seconds()) for center in centers)

    kept = sorted(segments, key=distance)[:limit]
    kept.sort(key=lambda item: getattr(item, "sequence", 0))
    return kept


def _brief(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _fmt(value: datetime | None) -> str:
    return value.isoformat(timespec="minutes") if value else "?"
