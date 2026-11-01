"""Dynamic bounded JEV questions. No generation, controls or persistence here."""
from .policies import choice

VERSION = 'jev-native-v3'
REPLY_INSTRUCTION = '按已接受的入口和短答许可生成自然短答。只输出Schema规定的reply，不选择档位、不重新辨人、不作工具或记忆承诺。'


def entry_questions(lines, schema):
    questions = {}
    for row in lines:
        i = row['i']
        candidates = {'unknown': '无法合理选择人物'}
        candidates.update({c['p']: '人物候选' for c in row.get('candidates', [])
                           if c.get('source') != 'temporary_voice'})
        if row.get('identity') != 'unresolved' and row.get('p'):
            candidates[row['p']] = '本句人物'
        allowed = schema['properties']['items']['items']['properties']['person']['enum']
        candidates = {p: v for p, v in candidates.items() if p in allowed}
        questions[f'p{i}'] = choice(f'I{i}是谁说的。结合本句声音、现场、连续性和有效视觉；同轨、惯常角色不能独立确认；暂归重复不升级', candidates)
        questions[f'c{i}'] = choice(f'I{i}人物确定度；confirmed须能区分其他合理来源，声音弱不自动推翻有效确认；unknown人物必须unknown',
                                   {'confirmed': '确认', 'tentative': '暂归', 'unknown': '未知'})
        questions[f'a{i}'] = choice(f'I{i}是否涉及匠石。依据点名、有效面向设备、语义问答或未转向的持续交谈；时间接近不单独授予许可。旧招呼、同话题、匠石自己的插话不能证明。uncertain须有具体关联而非仅对象未知；旁聊、媒体、无依据选other',
                                   {'direct': '明确交往', 'linked': '已有具体关联但有缺口', 'other': '背景或无关联依据'})
        questions[f'e{i}'] = choice(f'I{i}意思是否足够处理。问完等别人回答不是未完，ASR定稿不等于说完；不补写半句',
                                   {'complete': '完整可处理', 'incomplete': '必须等后文', 'uncertain': '完整度不明'})
    questions['effect'] = choice('对当前任务的控制，结合对象和语义；旁聊不控制匠石，无旧任务时明确新交往也可redirect',
                                 {'keep': '保持', 'stop': '停止表达', 'redirect': '转向', 'uncertain': '不明'})
    questions['work'] = choice('停止表达后是否还有独立操作，如取消下载；没有停止要求选none',
                               {'none': '无', 'required': '有操作', 'uncertain': '不明'})
    if len(questions) > 16:
        raise ValueError('question_limit')
    return questions


def pick(result, key, threshold):
    answer = result.answers.get(key, {})
    if result.status != 'success' or answer.get('confidence') is None or answer['confidence'] < threshold:
        raise ValueError('low_confidence:' + key)
    return answer['choice']


def decode_entry(result, lines, questions, threshold):
    selected, pending, items, relations, expressions = [], [], [], [], []
    for row in lines:
        i = row['i']; ref = row['evidence_ref']
        p,c,a,e = (pick(result, f'{k}{i}', threshold) for k in 'pcae')
        if p not in questions[f'p{i}']['criteria'] or (p == 'unknown') != (c == 'unknown'):
            raise ValueError('person_conflict')
        items.append({'i': i, 'person': p, 'certainty': c, 'why': '程序记录：原生人物选择，非模型解释'})
        relations.append(a); expressions.append(e)
        if a != 'other':
            if e == 'incomplete': pending.append(ref)
            else:
                selected.append(ref)
                if e == 'uncertain': pending.append(ref)
    effect = pick(result, 'effect', threshold); work = pick(result, 'work', threshold)
    if effect == 'uncertain' or work == 'uncertain': raise ValueError('control_uncertain')
    if effect != 'keep' and all(a == 'other' for a in relations):
        raise ValueError('background_control')
    route = 'prepare' if selected else 'wait' if pending else 'background'
    if effect == 'stop':
        route = 'prepare' if work == 'required' and selected else 'background'
        if work == 'required' and not selected: raise ValueError('operation_scope')
        if route == 'background': selected, pending = [], []
    elif work != 'none': raise ValueError('operation_without_stop')
    if effect == 'redirect' and route != 'prepare': raise ValueError('redirect_scope')
    interaction = 'direct' if 'direct' in relations else 'uncertain' if 'linked' in relations else 'none'
    complete = 'uncertain' if selected and pending else 'incomplete' if pending else 'complete'
    return dict(items=items, interaction=interaction, utterance_complete=complete, control=effect, route=route,
                process_refs=selected, pending_refs=pending, decision_refs=[r['evidence_ref'] for r in lines][:6],
                decision_reason='程序组合原生选择；原始答案与分数见阶段记录')


PREPARE = {
    'level': choice('按选中任务选择能力，不按人物重要性；查询、执行、修改、取消或交付工具结果需L1；不明选uncertain',
                    {'L1': '完整能力含工具', 'L2': '展开理解无工具', 'L3': '简短交往无工具', 'uncertain': '不明'}),
    'memory': choice('理解需要历史且当前材料不足才召回；不按暂归人物指定私人记忆', {'yes': '需要', 'no': '不需要', 'uncertain': '不明'}),
    'image': choice('主认知必须检查原图细节且图片可用才带图，旧观察不代表新画面', {'yes': '需要', 'no': '不需要', 'uncertain': '不明'}),
    'route': choice('只有许可齐备、身份确定、direct、redirect或空闲keep、无需工具历史原图或保存新事实的简单交往可短答；通用知识不要求人物经历，否则main',
                    {'main': '主认知', 'self_reply': '直接短答', 'uncertain': '不明'}),
    'ack': choice('有明确许可且接话有帮助才需要等待接话；不作答、不承诺、不为旁聊接话', {'yes': '可选接话', 'no': '不需要', 'uncertain': '不明'})}


def decode_prepare(result, threshold):
    level,memory,image,route,ack = (pick(result,k,threshold) for k in PREPARE)
    if 'uncertain' in (level,memory,image,route,ack): raise ValueError('preparation_uncertain')
    if route == 'self_reply' and (level != 'L3' or memory != 'no' or image != 'no'):
        raise ValueError('short_reply_conflict')
    return dict(route=route, level=int(level[1]), recall_memory=memory=='yes', main_image='include' if image=='yes' else 'omit',
                reply='', waiting_reply='', reason='程序组合原生处理需求；可选等待接话不阻塞主流程')
