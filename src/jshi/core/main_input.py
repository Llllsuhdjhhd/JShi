"""One main-flow representation for known/unknown and future input parts."""
IDENTITY_LEVELS = ("确定", "可能", "不太可能", "不确定")


def make_input(input_id, actor_id, display, text, *, source="text", level="不确定", evidence="", parts=(), number="", direction="", relevance=""):
    return {"number": number, "input_id": input_id, "actor_id": actor_id, "display": display,
            "text": text, "source": source,
            "identity_level": level if level in IDENTITY_LEVELS else "不确定",
            "identity_evidence": evidence, "parts": tuple(parts), "direction": direction, "relevance": relevance}


def render_inputs(items):
    rows = []
    media = {}
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
        refs = []
        for part in item.get('parts', ()):
            reference = part.get('reference')
            if not reference:
                continue
            key = (part.get('kind', ''), reference)
            code = media.setdefault(key, f'媒体{len(media) + 1}')
            if code not in refs:
                refs.append(code)
        if refs:
            rows.append("媒体关联（本次请求内）：" + "、".join(refs))
    return "\n".join(rows)


def entry_notes(text):
    import re
    return "\n".join(line for line in text.splitlines() if not re.match(r"^\d+\. ", line))


def entry_assessments(calls, input_ids):
    """Carry entry uncertainty to cognition without leaking batch-local references."""
    numbers = {key: str(n) for n, key in enumerate(input_ids, 1)}
    rows = []
    for call in calls:
        selected = [numbers[key] for key in call.get('input_ids', ()) if key in numbers]
        relation = {'direct': '入口认为与匠石有关，仍须核对依据',
                    'uncertain': '是否对匠石说尚不确定',
                    'none': '入口认为是背景，未建立对匠石的交往'}.get(call.get('interaction'))
        if not selected or not relation:
            continue
        # I references belong to each entry batch, not this merged main request.
        import re
        reason = str(call.get('decision_reason') or '')
        def reference(match):
            index = int(match[1]) - 1
            ids = call.get('input_ids', ())
            number = numbers.get(ids[index]) if 0 <= index < len(ids) else None
            return f'本轮第{number}条' if number else '该批其他发言'
        reason = re.sub(r'(?<![A-Za-z0-9])I(\d+)(?![A-Za-z0-9])', reference, reason)
        # Historical evidence is already available in the scene, but its aliases
        # are local to another request and must not collide with main references.
        reason = re.sub(r'(?<![A-Za-z0-9])[CEQ]\d+(?![A-Za-z0-9])', '入口所见历史材料', reason)
        rows.append('第' + '、'.join(selected) + '条：' + relation + '；依据与缺口：' + reason)
        for support in call.get('entry_support', ()):
            index = support.get('n', 0) - 1
            ids = call.get('input_ids', ())
            number = numbers.get(ids[index]) if 0 <= index < len(ids) else None
            if number and support.get('notes'):
                rows.append(f'第{number}条补充证据：' + '；'.join(support['notes']))
    if not rows:
        return ''
    return ('【本轮入口判断·可推翻，不是人物原话】\n' + '\n'.join(rows) +
            '\n身份确定不等于受话对象确定，进入主认知不等于需要开口。自己的插话及其摘要不能证明对方已与匠石交谈；须有对方具体承接或有效面对面依据。'
            '语音转写不保证字词及意图正确，不得把异常、叠音或含糊表达自动补成确定请求。对象未成立时可以不回应；明确对匠石说但关键意思不清时再澄清，不能按猜测承诺或执行。')
