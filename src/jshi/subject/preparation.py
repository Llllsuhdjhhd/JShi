"""Ephemeral analysis. Never calls experience, assembly, tools or scene writers."""
from dataclasses import dataclass, field
import hashlib
import json
import re
from time import monotonic
from uuid import uuid4

from jshi.models import ModelRequest
from jshi.core import SubjectState
from jshi.skill.base import parse_json_object

PREPARE_INSTRUCTION = '''你是匠石的后台回应准备。对方尚未说完，只整理已有证据、暂定目标、假设、缺口与候选回答，不预测后文为事实。
原话和历史材料分开，不混同人物，不把旧事当现在。不输出内部思维链，不执行工具，不承诺已查询、保存或交付。
只输出JSON：{"goal":"暂定目标","assumptions":["待确认条件"],"gaps":["缺口"],"candidate":"基于已有材料的候选内容"}。
候选仅是可修订工作稿；goal最多160字，每项最多160字，每组最多6项，candidate最多1200字。'''

LIGHT_INSTRUCTION = '''【提前准备与本次收尾】
user中的提前准备是未交付、可推翻的候选，不是人物原话或已确认事实。完整本轮输入优先。
核对新增条件、否定、引用、人物与目标变化；可复用有效证据，但不顺着旧候选解释新话。
若能可靠完成，按本次正常Schema输出完整回应；若需要重新深入分析或材料不足以轻量收尾，只输出{"need_deep":true}，不要先输出不完整实质答案。
本次没有义务说等待话，不声称候选内容已经让用户听到。
准备材料只有前段原话和候选，不含记忆编号。正式回忆只用本轮材料里的M编号。'''


class NeedDeep(Exception):
    """Internal escalation, caught before any voice or business effects."""


@dataclass
class PreparedAnalysis:
    subject_id: str
    object_id: str
    rows: tuple
    scene_version: int
    content: dict
    memory_rows: tuple = ()
    memory_versions: dict = field(default_factory=dict)
    created: float = field(default_factory=monotonic)
    trace_id: str = ''

    def compatible(self, subject_id, object_id, rows, scene_version):
        # Removing the memory array does not remove citations embedded in generated
        # candidate prose. Such a candidate cannot enter the formal M-only namespace.
        if re.search(r'(?<![A-Za-z0-9_])R[1-9]\d*(?![A-Za-z0-9_])',
                     json.dumps(self.content, ensure_ascii=False)):
            return False
        if (subject_id != self.subject_id or object_id != self.object_id or scene_version != self.scene_version
                or monotonic()-self.created > 45):
            return False
        current = {(r.start_ms, r.end_ms): r.text for r in rows}
        # Corrected or missing prefix invalidates preparation; not mere text-prefix guessing.
        return all(current.get((r.start_ms, r.end_ms)) == r.text for r in self.rows)

    def model_material(self, *, include_memories=True):
        body = {'prefix': [r.text for r in self.rows], 'preparation': self.content}
        if include_memories:
            # R codes stay inside preparation and the continuation decision.
            # The main cognition request uses the formal M codes instead.
            body['memories'] = [{'ref': f'R{i}', 'text': r.content or r.text,
                                  'occurred_at': r.occurred_at.isoformat(timespec='seconds') if r.occurred_at else None}
                                 for i, r in enumerate(self.memory_rows, 1)]
        return body


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def prepare(process, subject_id, object_id, rows, scene_version, allowed):
    from jshi.subject.cancellation import check, generation_scope, invoke
    with generation_scope(allowed):
        check()
        memories, versions = (), {}
        fn = getattr(process.memory, 'prepare_recall', None)
        if object_id and callable(fn):
            class ReadMemory:
                def generate(self, unused):
                    return fn(subject_id, '\n'.join(r.text for r in rows), object_id)
            # A slow read must not keep occupying the single formal cognition
            # executor after cancellation. Only the read survives, under the same cap.
            memories, versions = invoke(ReadMemory(), None)
        check()
        material = {'input': [r.text for r in rows],
            'memories': [{'ref': f'R{i}', 'text': r.content or r.text,
                          'occurred_at': r.occurred_at.isoformat(timespec='seconds') if r.occurred_at else None}
                         for i, r in enumerate(memories, 1)]}
        user = json.dumps(material, ensure_ascii=False, separators=(',', ':'))
        if len(user) > 10000:
            return None  # Never cut away a possibly essential opening condition.
        request = ModelRequest(purpose='voice_preparation', input_text=user, persona_user_text=user,
            subject_state=SubjectState(subject_id, '匠石', '提前准备'), system_extra=PREPARE_INSTRUCTION,
            json_output=True)
        # Bypass the cognition skill output schema; retain its raw transport/router.
        port = getattr(getattr(process.cognition, '_skill', None), '_model', process.cognition)
        trace_id = 'preparation-' + uuid4().hex
        process._record_step_input(request, port, subject_id=subject_id, activity_id=trace_id)
        raw = invoke(port, request)
        check()
        data = parse_json_object(raw.text)
        if set(data) != {'goal', 'assumptions', 'gaps', 'candidate'}:
            return None
        if any(not isinstance(data[k], str) or len(data[k]) > cap for k, cap in (('goal',160), ('candidate',1200))):
            return None
        if any(not isinstance(data[k], list) or len(data[k]) > 6 or
               any(not isinstance(v,str) or len(v)>160 for v in data[k]) for k in ('assumptions','gaps')):
            return None
        return PreparedAnalysis(subject_id, object_id, tuple(rows), scene_version, data,
                                tuple(memories), versions, trace_id=trace_id)
