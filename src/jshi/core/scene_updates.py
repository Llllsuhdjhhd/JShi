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

SCENE_UPDATE_NOTE = '''【认识更新】
每轮填写scene_updates，无更新填[]。新证据改变旧理解时写入（最多2条），不能只口头承认：scope写依据支持的范围，understanding写新理解，basis写新依据及被修正的旧理解。事件纠正不自动成为以后交往的规则。保留原话与已播事实，不把推测当事实。更新是内部事件，交写场整理；不是待说的话，不自动改档案或执行任务。'''

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
