"""One main-flow representation for known/unknown and future input parts."""
IDENTITY_LEVELS = ("确定", "可能", "不太可能", "不确定")


def make_input(input_id, actor_id, display, text, *, source="text", level="不确定", evidence="", parts=(), number="", direction="", relevance=""):
    return {"number": number, "input_id": input_id, "actor_id": actor_id, "display": display,
            "text": text, "source": source,
            "identity_level": level if level in IDENTITY_LEVELS else "不确定",
            "identity_evidence": evidence, "parts": tuple(parts), "direction": direction, "relevance": relevance}


def render_inputs(items):
    rows = []
    for i, item in enumerate(items, 1):
        source = {"text": "文字", "audio": "语音"}.get(item["source"], item["source"])
        number = str(item.get("number") or i).removeprefix("N")
        rows.append(f"{number}. {item['display']}：{item['text']}")
        note = f"来源：{source}；人物：{item['identity_level']}"
        if item.get("identity_evidence"):
            note += "；依据：" + item["identity_evidence"]
        direction = {'yes': '对匠石说', 'maybe': '可能对匠石说', 'no': '本批背景，并非请求回答'}.get(item.get('direction'))
        if direction:
            note += '；指向：' + direction
        rows.append("（输入标注·不是原话）" + note)
        refs = [p.get("reference", "") for p in item.get("parts", ()) if p.get("reference")]
        if refs:
            rows.append("材料引用：" + "、".join(refs))
    return "\n".join(rows)


def entry_notes(text):
    import re
    return "\n".join(line for line in text.splitlines() if not re.match(r"^\d+\. ", line))
