"""同一模型能力框架，不同的思考任务与输出契约。"""
from jshi.skill.base import Skill, SkillError


class MemoryThought(Skill[dict]):
    required = ()

    def parse(self, data):
        if any(key not in data for key in self.required):
            raise SkillError('missing thought output')
        for key in self.required:
            expected = str if key == 'standard' else list
            if not isinstance(data[key], expected):
                raise SkillError('invalid thought output')
        return dict(data)

    def _fallback(self, raw_text):
        raise SkillError('invalid reflection output; retain task for retry')


class RecallEvaluationThought(MemoryThought):
    name = 'recall_evaluation'
    required = ('feedback',)

    def parse(self, data):
        value = super().parse(data)
        value['feedback'] = [item for item in value['feedback'] if isinstance(item, dict)
                             and isinstance(item.get('event_id'), str)
                             and isinstance(item.get('judgment'), str)
                             and item.get('judgment') in {'helpful', 'irrelevant', 'misleading', 'neutral'}
                             and isinstance(item.get('reason'), str) and item['reason'].strip()]
        if not isinstance(value.get('coverage'), str) or value['coverage'] not in {'sufficient', 'thin', 'missing', 'unknown'}:
            value['coverage'] = 'unknown'
        value['gap_query'] = value.get('gap_query', '') if isinstance(value.get('gap_query', ''), str) else ''
        value['needs_reprocess'] = value.get('needs_reprocess') is True
        reason = value.get('reprocess_reason', '')
        value['reprocess_reason'] = reason.strip() if isinstance(reason, str) else ''
        return value

    instruction = """用途：评价一次回忆，为后续检索提供有依据的反馈。
输入：当时的问题、候选事件、主认知实际可见与使用声明、回应交付状态及相关后续交流。事件代号仅在本请求有效。
目标任务：
候选相关性、进入上下文、实际使用、确实帮助了结果是不同的事情。没有后续证据时只评相关性，
不要宣称解决了问题。只能评价材料里有 event_id 的事件，不对没看过的事件评分。
零结果不自动表示失败；旁聊可能无需回忆，询问旧约定却缺证据才可能有缺口。计划不等于已交付，未引用不等于无用，未知不当成否定。
只有材料支持纠正旧认识、出现冲突或新的关系线索时才建议再加工；仅仅被召回或不相关不构成新认识。
输出：
输出 feedback 数组，每项 event_id、judgment(helpful/irrelevant/misleading/neutral)、reason。
说明具体贡献或问题，主观判断保留不确定性。遗漏只能在理由中说明，不能编造事件 ID。
可补充 coverage(sufficient/thin/missing/unknown)、gap_query、needs_reprocess(布尔)、reprocess_reason；缺乏依据时coverage为unknown，needs_reprocess为false。"""
    schema = {'type': 'object', 'properties': {'feedback': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'event_id': {'type': 'string'},
        'judgment': {'enum': ['helpful', 'irrelevant', 'misleading', 'neutral']},
        'reason': {'type': 'string'}}, 'required': ['event_id', 'judgment', 'reason']}}},
        'required': ['feedback']}
    schema['properties'].update({
        'coverage': {'enum': ['sufficient', 'thin', 'missing', 'unknown']},
        'gap_query': {'type': 'string'}, 'needs_reprocess': {'type': 'boolean'},
        'reprocess_reason': {'type': 'string'},
    })


class RecallReprocessingThought(MemoryThought):
    name = 'recall_reprocessing'
    required = ('candidates',)
    instruction = """你在回顾几段旧经历，发现新关系或修正原来的理解。
输出 candidates 数组，每项 content(新认识)、source_ids(所引用的 event_id)、relation(关系与依据)。
保留‘我现在认为’的认识性质，区分事实、解释与联想；不能把推测改写成当年的事实。
不复制原事件，不因为被召回就生成新认识；没有新内容返回空数组。
如果材料都是自省产生的认识，不对它们递归生成另一层回忆事件。"""
    schema = {'type': 'object', 'properties': {'candidates': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'content': {'type': 'string'},
        'source_ids': {'type': 'array', 'items': {'type': 'string'}}, 'relation': {'type': 'string'}},
        'required': ['content', 'source_ids', 'relation']}}}, 'required': ['candidates']}


class PressureComparisonThought(MemoryThought):
    name = 'pressure_comparison'
    required = ('ordered_ids', 'standard', 'admit_ids', 'discard_ids', 'outlets', 'reasons')
    instruction = """评价匠石这次看到的自省候选。S 是认识，P 是人物范围，E 是来源。代号仅在本次有效。来源相同不等于独立佐证。
逐条看它比原经历多认识了什么，给出的依据支持到哪里，今后在哪种情境值得记住。个人经历本身的意义也可以构成理由。
有具体用途且依据足够的放进 admit_ids，并在 outlets 为每条选一个或多个去处。memory 保存值得再想起的认识；personal_experience 保存匠石自身可借鉴的经验；person_experience 保存与该 P 相处的经验，仅有明确人物范围时可选。仅选经验也可以。不要把一次交往概括为对方一贯如此。
只有复述、重复或缺乏保留理由的放进 discard_ids。其余留在试验区观察，说明还缺什么。两份名单都允许为空，二者不能重叠。容量有余也可以全部放弃。
ordered_ids 列出本次所有 S 各一次，按相对保留偏好排列。reasons 对每个 S 写 id 和 reason，交代该项去留的依据。推测要写明不确定性，不能声称见过未提供的原文或后续效果。
要求更新时，standard 用跨批可理解的文字总结取舍条件、与旧标准的变化及仍有疑问的地方，不写请求内代号。其余时候沿用旧标准。"""
    schema = {'type': 'object', 'properties': {
        'ordered_ids': {'type': 'array', 'items': {'type': 'string'}}, 'standard': {'type': 'string'},
        'admit_ids': {'type': 'array', 'items': {'type': 'string'}},
        'discard_ids': {'type': 'array', 'items': {'type': 'string'}},
        'outlets': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'id': {'type': 'string'}, 'kinds': {'type': 'array', 'items': {
                'enum': ['memory', 'personal_experience', 'person_experience']}}},
            'required': ['id', 'kinds']}},
        'reasons': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'id': {'type': 'string'}, 'reason': {'type': 'string'}}, 'required': ['id', 'reason']}}},
        'required': ['ordered_ids', 'standard', 'admit_ids', 'discard_ids', 'outlets', 'reasons']}
