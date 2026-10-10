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
    instruction = """你在评价一次回忆，以当时的问题和后续交流为依据。
候选相关性、进入上下文、实际使用、确实帮助了结果是不同的事情。没有后续证据时只评相关性，
不要宣称解决了问题。只能评价材料里有 event_id 的事件，不对没看过的事件评分。
输出 feedback 数组，每项 event_id、judgment(helpful/irrelevant/misleading/neutral)、reason。
说明具体贡献或问题，主观判断保留不确定性。遗漏只能在理由中说明，不能编造事件 ID。"""
    schema = {'type': 'object', 'properties': {'feedback': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'event_id': {'type': 'string'},
        'judgment': {'enum': ['helpful', 'irrelevant', 'misleading', 'neutral']},
        'reason': {'type': 'string'}}, 'required': ['event_id', 'judgment', 'reason']}}},
        'required': ['feedback']}


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
    required = ('ordered_ids', 'standard', 'admit_ids', 'reasons')
    instruction = """比较 A、B 两个压力容器的样例，按当前情境的相对保留偏好排序。
没有绝对价值分数。ordered_ids 必须包含本次全部样例 ID 各一次，从优先保留到优先让出空间。
跨批比较，考虑重复、证据、认识增量、适用情境及空间成本。不要只偏爱新内容或经常召回的内容。
需要更新标准时，standard 用语言描述取舍原则、适用条件、与旧标准的变化及不确定性；
不需要更新时沿用旧标准。reasons 数组逐项写 id、reason，解释实际排序，不事后编造理由。
admit_ids 是适合进入真实记忆的 ID 子集，按实际适用标准做宽松选择；不等于只留前几名。
标准尚在形成时可作临时接纳，但说明暂定理由。这里比较的是抽样材料，不能宣称评审了整个库。"""
    schema = {'type': 'object', 'properties': {
        'ordered_ids': {'type': 'array', 'items': {'type': 'string'}}, 'standard': {'type': 'string'},
        'admit_ids': {'type': 'array', 'items': {'type': 'string'}},
        'reasons': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'id': {'type': 'string'}, 'reason': {'type': 'string'}}, 'required': ['id', 'reason']}}},
        'required': ['ordered_ids', 'standard', 'admit_ids', 'reasons']}
