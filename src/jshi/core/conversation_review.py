"""Structured, provisional conversation review shared by prompt builders."""
from __future__ import annotations

import math
import re

REVIEW_SCHEMA = {
    "speaker_judgments": {"type": "array", "items": {"type": "object", "properties": {
        "n": {"type": "string", "description": "本批新发言编号，如N1"},
        "input_id": {"type": "string", "description": "原样复制该条发言的input_id，必须与n对应"},
        "speaker_pick": {"type": "string", "description": "提供的候选P代号或unknown/new"},
        "level": {"enum": ["确定", "可能", "不太可能", "不确定"]},
        "evidence": {"type": "string"}, "to_jiangshi": {"enum": ["yes", "maybe", "no"]},
        "address_reason": {"type": "string"},
        "semantic_pick": {"type": "string", "description": "仅按语义支持的候选P代号或unknown/new"},
        "semantic_reason": {"type": "string", "description": "简短语义依据"},
        "evidence_relation": {"enum": ["agree", "voice_only", "semantic_only", "conflict", "insufficient"]},
    }}},
    "reply_targets": {"type": "array", "items": {"type": "string"}},
    "next_jev_note": {"type": "string", "maxLength": 120},
}

# Legacy import name; main cognition must not use this instruction.
REVIEW_INSTRUCTION = """【独立人物判断】
依据候选资料、现场和交往历史提供后续JEV建议，输出speaker_judgments与next_jev_note。
不能产生本轮回应，不能直接修改人物归属、正式档案、声纹或已经处理的历史。
声音与语义冲突未解释时不能确定；候选资料不是身份已确认的证据。
"""


def parse_review(data):
    result = []
    seen = set()
    rows = data.get("speaker_judgments")
    for row in rows[:64] if isinstance(rows, (list, tuple)) else ():
        if not isinstance(row, dict):
            continue
        n = str(row.get("n") or "")
        if not re.fullmatch(r"N?[1-9]\d{0,2}", n):
            continue
        pick = str(row.get("speaker_pick") or "")
        level = row.get("level")
        if not (pick in {"unknown", "new"} or re.fullmatch(r"P\d+", pick)) or level not in {"确定", "可能", "不太可能", "不确定"}:
            continue
        n = "N" + n.removeprefix("N")
        if n in seen:
            continue
        seen.add(n)
        try:
            score = float(row.get("score", .5))
        except (ValueError, TypeError):
            score = .5
        if not math.isfinite(score):
            score = .5
        result.append({"n": "N" + n.removeprefix("N"), "speaker_pick": pick,
            "level": level, "score": max(0., min(1., score)),
            "evidence": str(row.get("evidence") or "")[:100],
            "to_jiangshi": row.get("to_jiangshi") if row.get("to_jiangshi") in {"yes", "maybe", "no"} else "maybe",
            "address_reason": str(row.get("address_reason") or "")[:100]})
        if row.get('input_id'):
            result[-1]['input_id'] = str(row['input_id'])
        for key, value in parse_evidence_support(row).items():
            result[-1][key] = value
        if result[-1].get("evidence_relation") == "conflict" and result[-1]["level"] == "确定":
            result[-1]["level"] = "可能"
        elif result[-1].get("evidence_relation") == "insufficient":
            result[-1]["level"] = "不确定"
            result[-1]["speaker_pick"] = "unknown"
    return {"speaker_judgments": tuple(result), "next_jev_note": str(data.get("next_jev_note") or "")[:120]}


def parse_reply_targets(data):
    raw = data.get("reply_targets")
    return tuple(dict.fromkeys(x for x in raw[:64] if isinstance(x, str) and re.fullmatch(r"[PS]\d+", x))) if isinstance(raw, list) else ()


def parse_evidence_support(row, voice=None, allowed=None):
    """Keep physical evidence supplied by the program separate from model semantics."""
    pick = str(row.get("semantic_pick") or "")
    if not (pick in {"unknown", "new"} or re.fullmatch(r"P\d+", pick)):
        pick = ""
    if allowed is not None and pick.startswith("P") and pick not in allowed:
        pick = ""
    relation = row.get("evidence_relation")
    if relation not in {"agree", "voice_only", "semantic_only", "conflict", "insufficient"}:
        relation = ""
    if voice is not None:
        physical = voice.get("pick", "")
        strength = voice.get("strength", "unavailable")
        if strength == "clear" and physical and pick and pick != "unknown" and physical != pick:
            relation = "conflict"
        elif strength == "unavailable" and relation in {"agree", "voice_only", "conflict"}:
            relation = "semantic_only" if pick and pick != "unknown" else "insufficient"
        elif relation == "agree" and (not pick or pick != physical):
            relation = "insufficient"
    return {"semantic_pick": pick, "semantic_reason": str(row.get("semantic_reason") or "")[:100],
            "evidence_relation": relation}
