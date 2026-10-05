"""Independent rich-context identity advice. JEV remains the entry decision maker."""
from __future__ import annotations

import json

from jshi.core import SubjectState
from jshi.core.conversation_review import REVIEW_SCHEMA, parse_review
from jshi.models import ModelRequest
from jshi.skill.base import parse_json_object


PERSON_REVIEW_INSTRUCTION = '''你是匠石的独立人物判断流程，不是快速JEV，也不是本轮主认知。只给后续JEV身份与指向建议，不产生回复、动作、工具、声纹登记或档案修改。
本批待核对发言以input_id和N编号对位；候选人物按给定P代号，S代号只表示声音连续性。只能选该发言允许的候选，或unknown/new；new是候选之外的可能性，不新建人物。
片场、未整理事件、近期交往、实际播放及候选资料是判断背景，不是本轮新输入。candidate_only资料属于候选本人，不证明当前说话人就是他。比较独立语义线索与程序给出的声音证据，输出semantic_pick、semantic_reason、evidence_relation和综合结论。对话接续只说明可能在回答，不单独证明姓名；话里提到某名字不等于自报。
既有登记声纹及人工关联受保护，自报仅是称呼线索。声音与语义冲突未解释时不得确定；都不足保持未知，不强行猜人。分数是相似度或工程参考，不是身份概率；同一猜测重复出现不是新增证据。
使用更多背景核对快速JEV的疑问，但不能假称重新听过音频；只根据提供的声纹证据与文本判断。已播出、部分播出、准备说严格区分，准备询问不是已经问过。
short_voice_hint及repeated_short_audio是同一短句首尾重复拼接的辅助相似度，不是身份概率；原始时长不变，重复不能作为新增独立证据，不能单靠它确定姓名或认作已确认声纹。
输出speaker_judgments及next_jev_note，给简短可核对依据，不写推理过程。next_jev_note最多120字，注明仍未解决的疑问。最终本次输入归属由入口/JEV结合新证据应用规则；你的答复不回写已处理轮次，不直接关联历史、档案或声纹。'''


class PersonReviewer:
    def __init__(self, model, process):
        self.model, self.process = model, process

    def request(self, subject_id, snapshot):
        payload = dict(snapshot['payload'])
        # Work on this separate worker; never run candidate-history reads on the
        # fast JEV/main-response path or change its recall/memory mappings.
        knowledge = []
        query = '\n'.join(u.text for u in snapshot['current'])[:4000]
        for oid, code in snapshot['candidate_codes'].items():
            profile = self.process.profiles.get(oid)
            if profile is None or profile.status == 'rejected':
                continue
            row = {'who': code, 'candidate_only': True, 'names': [profile.label, *profile.aliases]}
            portrait = getattr(self.process.memory, 'portrait', None)
            if callable(portrait):
                try:
                    value = portrait(subject_id, oid)
                    if isinstance(value, dict) and value.get('object_id') == oid and value.get('subject_id') == subject_id:
                        row['portrait'] = str(value.get('visible_summary') or '')[:2000]
                except Exception:
                    row['portrait_unavailable'] = True
            experience = getattr(self.process.long_term_experience, 'recall_experience', None)
            if callable(experience):
                try:
                    hits = experience(subject_id, query, object_ids=(oid,), budget_chars=2000)
                    row['experience'] = [{'text': hit.content[:2000], 'source_ids': list(hit.source_event_ids)}
                                         for hit in hits if hit.object_id == oid][:3]
                except Exception:
                    row['experience_unavailable'] = True
            manual = self.process.unknown_inputs
            if manual.path.is_file():
                row['manual_history'] = [{'input_id': hit['input_id'], 'text': hit['text']}
                                        for hit in manual.for_person(oid, query, limit=3, max_chars=2000)]
            knowledge.append(row)
        payload['candidate_knowledge'] = knowledge
        schema = {'type': 'object', 'properties': {key:value for key,value in REVIEW_SCHEMA.items() if key != 'reply_targets'}}
        user = json.dumps(payload, ensure_ascii=False, default=str)
        return ModelRequest(purpose='voice_person_review', input_text=user,
            subject_state=SubjectState(subject_id, '匠石', '独立核对人物，反馈后续JEV'),
            system_extra=PERSON_REVIEW_INSTRUCTION + '\n只输出JSON，Schema：' + json.dumps(schema, ensure_ascii=False),
            persona_user_text=user)

    def run(self, subject_id, snapshot, on_request=None):
        request = self.request(subject_id, snapshot)
        if on_request:
            on_request(request)
        raw = self.model.generate(request)
        snapshot['model_raw'] = raw.text
        data = parse_json_object(raw.text)
        if not isinstance(data.get('speaker_judgments'), list):
            raise ValueError('人物复核缺少speaker_judgments数组')
        return parse_review(data)
