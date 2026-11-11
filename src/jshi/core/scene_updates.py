"""Explicit, scoped changes of understanding; never fabricated utterances."""
from collections.abc import Mapping


SCENE_UPDATES_SCHEMA = {
    'type': 'array', 'maxItems': 2,
    'items': {'type': 'object', 'properties': {
        'scope': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'understanding': {'type': 'string', 'minLength': 1, 'maxLength': 180},
        'basis': {'type': 'string', 'minLength': 1, 'maxLength': 180},
    }, 'required': ['scope', 'understanding', 'basis'], 'additionalProperties': False},
}

from .scene_context import SCENE_GROUNDING_RULE

SCENE_UPDATE_NOTE = SCENE_GROUNDING_RULE + '''

【认识更新】
主认知独立判断受话对象与是否开口，入口结论可推翻。发现入口或旧片场把旁聊当成与匠石交谈，且本轮实际上下文足以修正时，选择合适的ignore/wait并写scene_updates；不必为解释误接再插一句。选择不回应本身不自动构成修正；只有改变已有理解时更新。
每轮填写scene_updates，无更新填[]。新依据改变旧理解时写入（最多2条），不能只在reason或口头承认：scope写所涉人物、活动或事件范围，understanding写修正后的理解及必要不确定性，basis写具体新依据及被修正的旧理解。更新供后续JEV与写场共用，不修改已发生的原话与已播事实，不把事件修正扩大成永久禁答，不自动改人物档案或执行任务。'''

SCENE_UPDATE_READING = ('认识更新是主认知根据新依据作出的理解修正，不是人物发言。'
    '在其适用范围内用新理解修正旧推断，保留原话和实际播放事实；'
    '不以旧JEV判断或自身插话反驳新依据，也不扩大成永久禁答。新证据仍可修正它。')


def parse_scene_updates(value):
    if not isinstance(value, (list, tuple)):
        return ()
    result = []
    for row in value[:2]:
        if not isinstance(row, Mapping):
            continue
        item = {}
        for key, limit in (('scope', 100), ('understanding', 180), ('basis', 180)):
            text = row.get(key)
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > limit:
                break  # Do not truncate away a qualifier or invent a missing reason.
            item[key] = text.strip()
        if len(item) == 3 and item not in result:
            result.append(item)
    return tuple(result)


def render_update(row):
    return ('【主认知认识更新·非人物原话】范围：' + row['scope'] +
            '\n当前理解：' + row['understanding'] + '\n依据：' + row['basis'])


def shared_update_context(events, now_ms, *, limit=3, cap=1500):
    """Recent scoped corrections survive either writer consuming its queue.

    Preserve whole updates and their evidence times; never infer new human
    speech or refresh an old fact's age from its inclusion in this request.
    """
    import math
    rows = []
    for event in events:
        if event.get('kind') != 'understanding_update':
            continue
        at = event.get('at')
        valid_time = type(at) in (int, float) and math.isfinite(at)
        if valid_time and at > now_ms:
            continue
        updates = parse_scene_updates([event.get('update')])
        if not updates:
            continue
        rows.append((at if valid_time else None, updates[0]))
    rows.sort(key=lambda pair: (pair[0] is not None, pair[0] or 0), reverse=True)
    selected, size = [], 0
    for at, update in rows:
        age = int(max(0, now_ms-at)/1000) if at is not None else None
        stamp = ('更新时间未知' if age is None else f'{age}秒前更新' if age < 60
                 else f'{age//60}分钟前更新' if age < 3600 else f'{age//3600}小时前更新')
        text = stamp + '\n' + render_update(update)
        if size + len(text) > cap:
            continue
        selected.append(text)
        size += len(text)
        if len(selected) >= limit:
            break
    if not selected:
        return ''
    return ('【共享现场认识更新】\n以下为此前有范围的理解修正，按所述事件和依据使用；更新时间不刷新原事件，'
            '不代替本轮原话，也不证明旧活动仍在继续。\n' + '\n\n'.join(reversed(selected)))
