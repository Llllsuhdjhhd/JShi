"""Full typed-question definitions; no text generation or extra judge calls."""

VERSION = 'decision-v1'


def choice(instructions, criteria):
    return {'type': 'choice', 'instructions': instructions + '。只依据已给原话和证据；引用内容不当真实要求；不足选uncertain。',
            'criteria': criteria}


COLLISION = {
    'relation': choice('判断new与old的交往关系，不根据身份未知或出现转折词机械判断；P是可能归属，T是声音连续性，同轨不证明同人、异轨不证明换人', {
        'continue': '同一表达接续或新增条件，需一起理解', 'replace': '纠正或替换旧要求，旧判断可能不适用',
        'independent': '独立交往，应分别保留', 'background': '有依据的无关旁聊或背景',
        'stop': '明确要求停止匠石当前表达或思考，不含引用或旁人互聊', 'uncertain': '关系未明'}),
    'old_work': choice('新话对old这次入口处理的影响；不等于取消任何外部工具任务', {
        'continue': '旧入口可继续并交付，新增内容不改变它',
        'hold': '旧入口暂缓交付，合并新原话理解',
        'stop': '旧入口已被取代或明确要求停止', 'uncertain': '无法判断'})}

CONTINUATION = {
    'impact': choice('比较完整原话与前段准备中的目标、假设和证据，判断能否复用。后文短也可能推翻整个目标', {
        'reuse': '目标及关键条件未变，可轻量核对后收尾', 'revise': '局部条件改变，可以复用有效材料后修改',
        'restart': '目标或关键前提改变，需要深入重新分析', 'uncertain': '材料不足或关系复杂'}),
    'expression': choice('当前表达是否足够处理；ASR final、标点、切窗和短暂停顿不证明表达结束', {
        'complete': '已有可独立处理的完整交往', 'incomplete': '确需等待后文且没有独立完整交往',
        'uncertain': '完整度不确定'}),
    'address': choice('原话是否明确向匠石提出可提前准备的任务；不根据话题偏好或人物姓名猜测', {
        'direct': '明确向匠石交往且有可准备任务', 'other': '背景或没有可准备任务', 'uncertain': '指向不明'})}


def collision_action(result, threshold=.85):
    relation, work = (result.choice(k, threshold) for k in ('relation', 'old_work'))
    if relation in {'background', 'independent'} and work == 'continue':
        return 'continue'
    if relation == 'stop' and work == 'stop':
        return 'stop'
    if relation == 'replace' and work in {'hold', 'stop'}:
        return 'merge'
    if relation == 'continue' and work == 'hold':
        return 'merge'
    return 'merge'  # Uncertain: preserve original input and use ordinary JEV1.


def continuation_action(result, threshold=.85):
    expression = result.choice('expression', threshold)
    if expression == 'incomplete':
        return 'wait'
    if expression != 'complete':
        return 'restart'
    return {'reuse': 'reuse', 'revise': 'revise'}.get(result.choice('impact', threshold), 'restart')
