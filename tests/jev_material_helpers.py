"""Read actual material blocks in fixtures; accepts historical JSON fixtures too."""
import json
import re


def read_material(text):
    if text.startswith('{'):
        return json.loads(text)
    result = {}
    names = {'本批新事件': 'input_output', '前序依据': 'recent_context', '人物依据': 'identity_evidence',
             '主认知参考': 'main_scene', '人物映射': 'people', '历史候选': 'history_candidates',
             '历史参考': 'history_reference'}
    for title, value in re.findall(r'^【([^】]+)】\n(.*?)(?=\n\n【|\Z)', text, re.S | re.M):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value
        if title == '上一版当前现场':
            result.update(parsed)
        elif title == '材料范围':
            result['now'] = parsed.pop('now')
            result['time_basis'] = parsed.pop('time_basis', None)
            result['window'] = parsed
        else:
            result[names.get(title, title)] = parsed
    return result
