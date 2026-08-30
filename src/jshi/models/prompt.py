"""HTTP 认知提示：稳定内容进 system，本轮材料进 user。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from jshi.models.base import ModelRequest, ModelSpeaker


def is_name_ambiguous(reason: str) -> bool:
    return (reason or "").startswith("ambiguous_names")


def _identity_opening(summary: str) -> str:
    """组装身份一行。来源用逗号，并接上「以谁的身份回应」。"""
    text = (summary or "").strip()
    if text.startswith("你是"):
        text = text[2:].lstrip()
    text = text.replace("；来源：", "，来源：")
    display = text if text.startswith("你是") else f"你是{text}"
    if display.endswith("。"):
        display = display[:-1]
    rest = display[2:] if display.startswith("你是") else display
    name = rest.split("，")[0].split("；")[0].strip() or "自己"
    return f"{display}。你以{name}的身份回应对方，不是在讨论、也不是评审。"


def _ambiguous_speaker_note(speaker: ModelSpeaker) -> str:
    aliases = tuple(item.strip() for item in speaker.aliases if item and item.strip())
    if aliases:
        who = f"当前这一位别名：{'、'.join(aliases)}。"
    else:
        who = "当前先按这一位。"
    return f"此名字有多份档案，未消歧。{who}不要当成世界上只有这一个人。"


def _is_cognition(request: ModelRequest) -> bool:
    return bool((request.system_extra or "").strip()) or request.purpose == "subject_activity"


_WEEKDAYS = "一二三四五六日"


def _now_label(now: datetime | None) -> str:
    """把“现在”渲染成本地时间锚点，供模型推算“今天 / 昨天”。"""
    if now is None:
        return ""
    local = now.astimezone() if now.tzinfo is not None else now
    return (
        f"【当前时间】{local:%Y-%m-%d %H:%M}"
        f"（周{_WEEKDAYS[local.weekday()]}）"
    )


def _time_label(raw: object) -> str:
    """把 ISO 时间戳渲染成紧凑的本地时间标签（无则空）。"""
    if not raw:
        return ""
    try:
        value = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return ""
    local = value.astimezone() if value.tzinfo is not None else value
    return f"{local:%Y-%m-%d %H:%M}"


def build_system(request: ModelRequest) -> str:
    if not _is_cognition(request):
        return _short_system(request)
    parts: list[str] = []
    now_label = _now_label(request.now)
    if now_label:
        parts.append(now_label)
    extra = (request.system_extra or "").strip()
    if extra:
        parts.append(extra)
    state = request.subject_state
    commitments = [
        str(item).strip() for item in state.commitments if str(item).strip()
    ]
    if commitments:
        parts.append("【承诺】\n" + "\n".join(commitments))
    boundaries = _texts_of_kind(request.context, "boundary")
    if boundaries:
        parts.append("【边界】\n" + "\n".join(boundaries))
    return "\n\n".join(parts)


def build_user(request: ModelRequest) -> str:
    if not _is_cognition(request):
        return request.input_text
    speaker = request.speaker
    label = speaker.label.strip() if speaker and speaker.label.strip() else "对方"
    parts: list[str] = [f"【说话人】{label}"]
    if speaker is not None and is_name_ambiguous(speaker.reason):
        parts.append(_ambiguous_speaker_note(speaker))
    elif speaker is not None and speaker.status != "confirmed":
        parts.append(f"档案暂定，无冲突按此人说话，不要问你是{label}吗。")
    segments = _segments(request.context)
    if segments:
        parts.append(
            "【活跃区】\n"
            + "\n".join(
                _render_ref(sid, slabel, text, time_label)
                for sid, slabel, text, time_label in segments
            )
        )
    memories = _memories(
        request.context, {text for _sid, _slabel, text, _time in segments}
    )
    if memories:
        parts.append(
            "【回忆】\n"
            + "\n".join(
                _render_ref(mid, mlabel, text, time_label)
                for mid, mlabel, text, time_label in memories
            )
        )
    parts.append(f"【本轮】\n{label}：{request.input_text}")
    return "\n".join(parts)


def _short_system(request: ModelRequest) -> str:
    state = request.subject_state
    lines: list[str] = []
    if (state.identity_summary or "").strip():
        lines.append(state.identity_summary.strip())
    if (state.current_stance or "").strip():
        lines.append(f"当前立场：{state.current_stance.strip()}")
    return "\n".join(lines)


def _texts_of_kind(context: Sequence[Mapping[str, Any]], kind: str) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()
    for item in context:
        if str(item.get("kind") or "") != kind:
            continue
        content = str(item.get("content") or "").strip()
        if not content or content in seen:
            continue
        seen.add(content)
        texts.append(content)
    return texts


def _render_ref(ref_id: str, label: str, text: str, time_label: str = "") -> str:
    prefix = ref_id
    if time_label:
        prefix = f"{ref_id}[{time_label}]"
    if label:
        return f"{prefix}（{label}）：{text}"
    return f"{prefix}：{text}"


def _segments(
    context: Sequence[Mapping[str, Any]],
) -> list[tuple[str, str, str, str]]:
    for item in context:
        if item.get("kind") != "active_zone_refs":
            continue
        rows: list[tuple[str, str, str, str]] = []
        seen_ids: set[str] = set()
        for row in item.get("segments") or ():
            if not isinstance(row, dict):
                continue
            segment_id = str(row.get("id") or "").strip()
            text = str(row.get("text") or "").strip()
            segment_label = str(row.get("label") or "").strip()
            time_label = _time_label(row.get("occurred_at"))
            if (
                not segment_id
                or not text
                or segment_id.startswith("context-v")
                or segment_id in seen_ids
            ):
                continue
            seen_ids.add(segment_id)
            rows.append((segment_id, segment_label, text, time_label))
        return rows
    return []


def _memories(
    context: Sequence[Mapping[str, Any]],
    segment_texts: set[str],
) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    seen_ids: set[str] = set()
    for item in context:
        kind = str(item.get("kind") or "")
        source = str(item.get("source") or "")
        if kind != "recall_excerpt" and source != "memory":
            continue
        memory_id = str(item.get("id") or "").strip()
        text = str(item.get("content") or "").strip()
        memory_label = str(item.get("label") or "").strip()
        time_label = _time_label(item.get("occurred_at"))
        if not memory_id or not text or text in segment_texts or memory_id in seen_ids:
            continue
        seen_ids.add(memory_id)
        rows.append((memory_id, memory_label, text, time_label))
    return rows
