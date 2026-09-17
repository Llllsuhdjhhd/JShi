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


# --------------------------------------------------------------------------- #
# 相对时间标签（只改提示词里怎么显示，不动存储的精确时间）
#
# 分档：<1 小时按分钟；<72 小时按小时；<30 天按天；<1 年按周/月；更早按年。
# 这些都是「只影响提示词显示」的魔法数，可整段替换而不动任何存储。
# --------------------------------------------------------------------------- #


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _as_aware(value: datetime) -> datetime:
    """无时区按本地墙钟；已有时区保持原样。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=_local_tz())
    return value


def relative_time_label(
    raw: object,
    *,
    now: datetime | None = None,
) -> str:
    """把发生时间渲染成相对标签（「5分钟前」「3小时前」「1周前」「2个月前」）。

    只用于提示词显示；精确时间仍存在存储里（回忆的 ``occurred_at``、块的 ``at``）。
    时间落在未来时退回绝对标签；无法解析则空串。
    不带时区的时间按**本地**算（记忆后端 REMS 存的是本地墙钟），不要当成 UTC。
    """
    if not raw:
        return ""
    if isinstance(raw, datetime):
        value = raw
    else:
        try:
            value = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return ""
    anchor = now or datetime.now().astimezone()
    value = _as_aware(value)
    anchor = _as_aware(anchor)
    minutes = (anchor - value).total_seconds() / 60.0
    if minutes < 0:
        return _time_label(value)
    if minutes < 60:
        # 1 小时内按分钟报；不足 1 分钟也算 1 分钟，不出「0分钟前」。
        return f"{max(1, int(minutes))}分钟前"
    if minutes < 120:
        return "1小时前"
    if minutes < 72 * 60:
        hours = minutes / 60.0
        if hours < 3:
            return "2小时前"
        if hours < 6:
            return "3小时前"
        if hours < 24:
            return "半天前"
        if hours < 48:
            return "1天前"
        return "2天前"
    days = minutes / (60 * 24)
    if days < 2:
        return "1天前"
    if days < 30:
        # 天档到 3 周：按「就近的档位」报，不编造精确天数
        if days < 4:
            return "3天前"
        if days < 6:
            return "5天前"
        if days < 12:
            return "1周前"
        if days < 19:
            return "2周前"
        return "3周前"
    if days < 60:
        return "1个月前"
    if days < 120:
        return "2个月前"
    if days < 240:
        return "半年前"
    if days < 400:
        return "1年前"
    years = days / 365.0
    return f"{max(2, int(round(years)))}年前"


def _time_label(raw: object) -> str:
    """把发生时间渲染成紧凑的本地时间标签（无则空）。

    接受 ``datetime`` 或 ISO 字符串。
    """
    if not raw:
        return ""
    if isinstance(raw, datetime):
        value = raw
    else:
        try:
            value = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return ""
    local = _as_aware(value).astimezone()
    return f"{local:%Y-%m-%d %H:%M}"


def format_memory_line(
    content: str,
    *,
    label: str = "",
    occurred_at: object = None,
    now: datetime | None = None,
) -> str:
    """人格/组装侧回忆一行：可选 ``[时间]（名字）正文``；时间是相对标签。"""
    text = (content or "").strip()
    if not text:
        return ""
    when = relative_time_label(occurred_at, now=now)
    name = (label or "").strip()
    if when and name:
        return f"[{when}]（{name}）{text}"
    if when:
        return f"[{when}]{text}"
    if name:
        return f"（{name}）{text}"
    return text


STIMULUS_SPEECH = "speech"
STIMULUS_IDLE = "idle"


def format_turn_input(label: str, text: str, *, stimulus: str = STIMULUS_SPEECH) -> str:
    """【本轮】/【此时的输入】正文。闲时直写时间流过，不写成「名字：原话」。"""
    name = (label or "").strip() or "对方"
    if stimulus == STIMULUS_IDLE:
        return f"和上次{name}说话又过了3分钟"
    return f"{name}：{text}"


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
    governing = tuple(getattr(request, "governing_rules", ()) or ())
    if governing:
        parts.append("【附加规则】\n" + "\n".join(f"- {rule}" for rule in governing))
    return "\n\n".join(parts)


def build_user(request: ModelRequest) -> str:
    if not _is_cognition(request):
        return request.input_text
    persona = (getattr(request, "persona_user_text", "") or "").strip()
    if persona:
        return persona
    speaker = request.speaker
    label = speaker.label.strip() if speaker and speaker.label.strip() else "对方"
    parts: list[str] = [f"【说话人】{label}"]
    if speaker is not None and is_name_ambiguous(speaker.reason):
        parts.append(_ambiguous_speaker_note(speaker))
    elif speaker is not None and speaker.status != "confirmed":
        parts.append(f"档案暂定，无冲突按此人说话，不要问你是{label}吗。")
    segments = _segments(request.context, now=request.now)
    if segments:
        parts.append(
            "【活跃区】\n"
            + "\n".join(
                _render_ref(sid, slabel, text, time_label)
                for sid, slabel, text, time_label in segments
            )
        )
    memories = _memories(
        request.context,
        {text for _sid, _slabel, text, _time in segments},
        now=request.now,
    )
    if memories:
        parts.append(
            "【回忆】\n"
            + "\n".join(
                _render_ref(mid, mlabel, text, time_label)
                for mid, mlabel, text, time_label in memories
            )
        )
    extra = (getattr(request, "tool_input", "") or "").strip()
    if extra:
        parts.append(extra)
    # tool_hot_state 已并入【工具相关】（tool_input）；保留读取以免旧请求丢字段。
    hot = (getattr(request, "tool_hot_state", "") or "").strip()
    if hot and "【工具相关】" not in extra:
        parts.append(hot)
    parts.append("【本轮】\n" + format_turn_input(
        label, request.input_text, stimulus=getattr(request, "stimulus", STIMULUS_SPEECH)
    ))
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
    *,
    now: datetime | None = None,
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
            time_label = relative_time_label(row.get("occurred_at"), now=now)
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
    *,
    now: datetime | None = None,
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
        time_label = relative_time_label(item.get("occurred_at"), now=now)
        if not memory_id or not text or text in segment_texts or memory_id in seen_ids:
            continue
        seen_ids.add(memory_id)
        rows.append((memory_id, memory_label, text, time_label))
    return rows


