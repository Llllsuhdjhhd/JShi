"""自省结果的去留判断。评价方返回建议，持久层独立检查来源、容量和写入范围。"""
from __future__ import annotations

from dataclasses import dataclass

from jshi.core.params import reflection_param as param
from jshi.skill.base import SkillError
from .memory_store import dump
from .port import IntrospectionAnswer
from .thoughts import PressureComparisonThought

EXPERIENCE_OUTLETS = ('personal_experience', 'person_experience')
RETAINED_OUTLETS = ('memory', *EXPERIENCE_OUTLETS)


@dataclass(frozen=True)
class RetentionDecision:
    ordered: tuple[str, ...]
    admit: tuple[str, ...]
    discard: tuple[str, ...]
    outlets: dict[str, tuple[str, ...]]
    reasons: dict[str, str]
    standard: str

    def validate(self, visible, *, update_standard):
        expected = set(visible)
        for group in (self.ordered, self.admit, self.discard):
            if len(set(group)) != len(group) or not set(group) <= expected:
                raise SkillError('invalid or duplicate retention reference')
        if (set(self.ordered) != expected or set(self.reasons) != expected
                or set(self.admit) & set(self.discard) or set(self.outlets) != set(self.admit)
                or any(not kinds or len(set(kinds)) != len(kinds) or not set(kinds) <= set(RETAINED_OUTLETS)
                       for kinds in self.outlets.values())
                or not all(isinstance(reason, str) and reason.strip() for reason in self.reasons.values())
                or not isinstance(self.standard, str)
                or (update_standard and not self.standard.strip())
                or len(self.standard) > int(param('REFLECTION_STANDARD_CHARS'))):
            raise SkillError('incomplete or conflicting retention decision')


class ReflectionResultEvaluator:
    """替换此策略可改变候选出口、试验去留和正式记忆接纳。"""

    comparison_skill = PressureComparisonThought

    def behavior_outlets(self, answer: IntrospectionAnswer):
        if answer.degraded or answer.mode != 'model':
            return []
        outlets = []
        if answer.worth_learning and any(isinstance(item.get('content'), str)
                                         and item['content'].strip() for item in answer.candidates):
            outlets.append('learn')
        if answer.tool_need:
            outlets.append('tool')
        if answer.interaction_need:
            outlets.append('commitment')
        return outlets

    def recall_proposals(self, candidates, events):
        originals = {text.strip() for event in events for text in (event.get('content'), event.get('text'))
                     if isinstance(text, str) and text.strip()}
        return [item for item in candidates if isinstance(item, dict)
                and isinstance(item.get('content'), str) and item['content'].strip()
                and item['content'].strip() not in originals
                and isinstance(item.get('relation'), str) and item['relation'].strip()]

    def compare(self, sample, previous_standard, update_standard, ask):
        actors, sources = {}, {}
        samples = []
        codes = {}
        for row in sample:
            item = row['item']
            code = f'S{len(samples) + 1}'
            codes[code] = row['id']
            entry = {'id': code, 'content': item['content'], 'relation': item.get('relation', '')}
            actor = item.get('person_scope')
            if actor:
                entry['person'] = actors.setdefault(actor, f'P{len(actors) + 1}')
            entry['sources'] = [sources.setdefault(ref, f'E{len(sources) + 1}') for ref in item['source_ids']]
            if item.get('evidence_excerpts'):
                entry['excerpts'] = {sources[ref]: excerpt for ref, excerpt in item['evidence_excerpts'].items()}
            samples.append(entry)
        payload = {'samples': samples, 'previous_standard': previous_standard, 'update_standard': update_standard}
        budget = int(param('REFLECTION_CONTEXT_CHARS'))
        while len(dump(payload)) > budget and len(samples) > 1:
            codes.pop(samples.pop()['id'])
        incomplete = False
        if len(dump(payload)) > budget:
            entry = samples[0]
            entry.pop('excerpts', None)
            payload['previous_standard'] = previous_standard[:budget // 4]
            if len(dump(payload)) > budget:
                incomplete = True
                entry['sources'] = entry['sources'][:4]
                entry['content'] = entry['content'][:budget // 6]
                entry['relation'] = entry['relation'][:budget // 6]
                entry['partial'] = True
        answer = ask(payload)

        def resolve(values):
            if not isinstance(values, list) or any(not isinstance(v, str) or v not in codes for v in values):
                raise SkillError('retention decision refers to an unseen sample')
            return tuple(codes[v] for v in values)

        reasons = answer['reasons']
        if any(not isinstance(r, dict) or not isinstance(r.get('id'), str) or r['id'] not in codes
               for r in reasons) or len(reasons) != len(codes):
            raise SkillError('invalid retention reasons')
        outlets = {}
        people = {row['id']: row['item'].get('person_scope') for row in sample}
        for item in answer['outlets']:
            if not isinstance(item, dict):
                raise SkillError('invalid reflection outlet')
            ident, = resolve([item.get('id')])
            kinds = item.get('kinds')
            if (ident in outlets or not isinstance(kinds, list)
                    or any(not isinstance(kind, str) for kind in kinds)
                    or ('person_experience' in kinds and not people[ident])):
                raise SkillError('invalid or unscoped reflection outlet')
            outlets[ident] = tuple(kinds)
        decision = RetentionDecision(
            ordered=resolve(answer['ordered_ids']), admit=resolve(answer['admit_ids']),
            discard=resolve(answer['discard_ids']),
            outlets=outlets,
            reasons={codes[r['id']]: r.get('reason') for r in reasons}, standard=answer['standard'])
        decision.validate(codes.values(), update_standard=update_standard)
        if incomplete and (decision.admit or decision.discard):
            # 单条都放不全时只能待观察，不能依据截断的文字作最终取舍。
            key, = decision.ordered
            return RetentionDecision((key,), (), (), {}, {key: '材料因预算截短，留待完整比较。'}, decision.standard)
        return decision
