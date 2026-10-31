"""Atomic entry questions; the caller combines answers, never generated actions."""
from .policies import choice

ENTRY_VERSION = 'entry-v2'
ENTRY = {
    'address': choice('判断当前原话的交流对象。结合点名、实际问答、有效视听及场景变化；同话题、已知身份或匠石自己插过话不证明交往。例：匠石问开门时间，稍后答七点可承接；家人问同题则不指向匠石。无人承接且家人问答继续可反证旧关系，单独沉默不能判错；引用、媒体与旁聊不当指令', {
        'direct': '明确面向匠石的交往', 'other': '有依据的旁聊或背景', 'uncertain': '关系未明'}),
    'expression': choice('判断本次表达是否足够独立处理；ASR final、标点、切窗不证明说完，不补全半句', {
        'complete': '已有可独立处理的内容', 'incomplete': '确需后文且没有独立完整内容', 'uncertain': '完整度不明或范围混合'}),
    'task_effect': choice('判断对匠石当前思考或表达的影响；按对象和语义，不匹配停止词；无旧任务时明确新交往也用redirect', {
        'keep': '保持当前任务', 'stop': '明确停止当前思考或表达',
        'redirect': '新交往、补充或纠正需要转向', 'uncertain': '影响不明'}),
    'work_after_stop': choice('若要求匠石停止表达，判断是否还有需下游处理的独立操作，例如取消下载；未要求停说时选none', {
        'none': '无独立操作', 'required': '还有独立操作需处理', 'uncertain': '操作范围不清'})}


def entry_action(result, refs, thresholds):
    """None means bounded split fallback. Never discard uncertain input."""
    address = result.choice('address', thresholds['address'])
    expression = result.choice('expression', thresholds['expression'])
    effect = result.choice('task_effect', thresholds['stop'])
    work = result.choice('work_after_stop', thresholds['address'])
    route = None
    if address == 'direct' and effect == 'stop':
        if work == 'none':
            route = 'background'
        elif work == 'required' and expression == 'complete':
            route = 'prepare'
    elif address == 'other' and effect == 'keep' and expression == 'complete':
        if result.choice('address', thresholds['background']) == 'other':
            route = 'background'
    elif address == 'direct' and expression == 'incomplete' and effect == 'keep':
        if result.choice('expression', thresholds['wait']) == 'incomplete':
            route = 'wait'
    elif address == 'direct' and expression == 'complete' and effect in {'keep', 'redirect'}:
        route = 'prepare'
    if route is None:
        return None
    return {'interaction': 'direct' if address == 'direct' else 'none',
            'utterance_complete': expression, 'control': effect, 'route': route,
            'process_refs': list(refs) if route == 'prepare' else [],
            'pending_refs': list(refs) if route == 'wait' else [],
            'decision_reason': f'程序组合决策入口：{address}／{expression}／{effect}，去向{route}',
            'decision_refs': list(refs)[:6]}
