"""Structured, provisional conversation review shared by prompt builders."""
from __future__ import annotations

import math
import re

REVIEW_SCHEMA = {
    "speaker_judgments": {"type": "array", "items": {"type": "object", "properties": {
        "n": {"type": "string", "description": "本批新发言编号，如N1"},
        "speaker_pick": {"type": "string", "description": "提供的候选P代号或unknown/new"},
        "level": {"enum": ["确定", "可能", "不确定"]},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "string"}, "to_jiangshi": {"enum": ["yes", "maybe", "no"]},
        "address_reason": {"type": "string"},
        "semantic_pick": {"type": "string", "description": "仅按语义支持的候选P代号或unknown/new"},
        "semantic_reason": {"type": "string", "description": "简短语义依据"},
        "evidence_relation": {"enum": ["agree", "voice_only", "semantic_only", "conflict", "insufficient"]},
    }}},
    "reply_targets": {"type": "array", "items": {"type": "string"}},
    "next_jev_note": {"type": "string", "maxLength": 120},
}

REVIEW_INSTRUCTION = """【人物复判与下一轮接续】
【此时的输入】是本轮新内容的唯一入口。本批发言若带识别标注，它只说明本批人物候选、声音证据与JEV初判，不是人物原话。过往交往从已经写好的片场和回忆理解，不另装载原始前文、上轮回应或上轮JEV工作评价作为输入。
逐条分清是谁说、在对谁说、这一拍是否要回应。人物候选用P代号，可靠的临时声音用S代号，本批新发言用N编号。声音代号不等于人物身份；没有可靠声音关联的待定发言，不因名字相同而串成一个人。
JEV的needs_main_review是整批请求，不是逐条强制重判的开关。请求为true时重点核对本批影响交往的冲突、不确定或指向疑问；没有疑问且证据一致的条目不必重复人物判断。false也不表示初判不可修正，完整上下文出现反证时仍须核对。JEV的yes/maybe/no均为初判，no有明确反证时可以修正；maybe结合上下文判断，证据仍不足时可保留maybe，不强凑yes或no。是否开口最终由mode、reply及回应对象决定。
有效的主流程复判用于本轮处置，可修正JEV的上下文归属；改变初判须写简短依据。可能、未知或未解决的冲突不能变成已确认身份，不能仅凭主流程给出一个名字就覆盖已有声纹、自我介绍或人工关联。明确无关且并非对匠石说的新发言已从本批剔除；相关背景仍须结合片场判断，不将JEV的相关性标签当作必然正确。
JEV的n与本批N编号对应（n=3即N3）；P代号共用同一套人物映射，不能另编号。S代号只标声音连续性，不是已确认人物；上轮反馈中也可能带S代号，不据此猜姓名。合并批次后只使用本次提供的编号，不拿上轮N编号对应本批条目。
对未确定或只经JEV暂定的发言，比较本条提供的候选、unknown（仍不能归属）与new（更像候选之外的人）。结合原话接续、明确自我介绍、直接称呼与已有声音证据，输出speaker_judgments；没有需要复判的发言写[]。每项写n、speaker_pick、level、score、evidence、to_jiangshi、address_reason。原因只写简短证据，不写推理过程。
声纹分数是相似度，JEV和你的score是参考强度，都不是已校准概率。不能因JEV与你重复同一个猜测就提高确认程度。仅靠提到某人、知道某人经历或说话习惯，不足以确认是谁。没有依据时保持unknown，仍可正常对话。
人物判断共同考虑两类不同来源的证据：voice_evidence是程序给出的声音特征支持，semantic_pick与semantic_reason只写原话和上下文支持谁。speaker_pick、level、score、evidence是综合结果，不把两类分数机械相乘。两边独立支持同一人时可提高可信度；声音弱但语义明确时可主要采用语义，声音明确但语义缺少线索时可主要采用声音；两边都弱则保持未知。evidence_relation写agree（两边支持同一人）、voice_only（主要靠声音）、semantic_only（主要靠语义）、conflict（存在尚未解释的冲突）或insufficient（都不足）。声音偏向不同人但证据弱时可解释为何更采信语义；明确冲突未消除时，不给“确定”。不要把近期人物候选、自我介绍或上轮JEV短评冒充新的物理声音证据。主流程只核对已提供的声音证据，不重新听音频。
语义身份依据须能区分是谁，不能用输入中已经标出的姓名或声纹结论反过来证明语义身份。纠正你、接着你的提问说，只说明交往接续或对话指向；若不能据此区分人物，semantic_pick仍写unknown。明确声纹足以支持综合归属时可用voice_only，不为了“两边一致”编造第二份身份依据。
已明确自我介绍或人工关联，不因文本推测自动推翻。已匹配声纹若与新证据冲突，指出冲突并保留不确定；不要宣称你重新识别过声纹。候选肖像只是参考，未确认归属前不能把候选私人经历安到待定声音上。
本批若有identity_confirmation，以用户对声音的人工关联为人物归属依据；旧临时访客不是另一个已确认姓名的人，不拿它反复否认人工关联。未达到领先差值不等于最高相似度未达到门槛，声纹未能确认不等于确认是另一个人。解释身份依据时只说材料实际支持的结论，不猜设备、环境或样本未并入等故障原因。
“待定声音”仅表示程序正在收集多段样本，未确定姓名，也未认定出现新人。稳定的临时声音组只表示多段声音较一致，仍不等于已确认人物身份；不因几轮都用了同一代号就提高姓名确认程度。可以正常交流，不反复要求对方证明身份。
一个人可以有姓名、旧名、昵称等多个称呼；不同的人也可能重名。按人物代号区分，不因同名合并，不因名字不同拆成新人。人物怎么称呼匠石也可能有多种叫法，按提供的称呼资料理解。别人说“我也叫小琴”与“我叫你小琴”意义不同，后者不是自报姓名。
reply_targets写本轮要回应的已提供P或可靠S代号；空数组表示面向现场。复判与回应对象在同一次主认知里交出，不额外查询一次模型。无身份疑问时不必重复猜已确认的人。
next_jev_note留给下一次JEV一两句评价：点明人物或声音、判断与简短原因，保留可能或未定，最多120字。没有需延续的评价写空字符串。它不是reply、不是unsaid、不是对外说过的话；ignore、wait、think也可以有短评。
下一轮仍结合原话和新证据修正评价，不把上轮猜测当成确认，不自行建立人物、登记声纹或改写旧记忆。已播出、部分播出和准备说的内容以交付事实为准；未播出内容不能作为对方已听见的依据。
输出按本次Schema，将新增字段并入同一个JSON对象；没有提供复判材料时仍按正常交往回应。
"""


def parse_review(data):
    result = []
    seen = set()
    rows = data.get("speaker_judgments")
    for row in rows[:64] if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        n = str(row.get("n") or "")
        if not re.fullmatch(r"N?[1-9]\d{0,2}", n):
            continue
        pick = str(row.get("speaker_pick") or "")
        level = row.get("level")
        if not (pick in {"unknown", "new"} or re.fullmatch(r"P\d+", pick)) or level not in {"确定", "可能", "不确定"}:
            continue
        n = "N" + n.removeprefix("N")
        if n in seen:
            continue
        seen.add(n)
        try:
            score = float(row.get("score", .5))
        except (ValueError, TypeError):
            score = .5
        if not math.isfinite(score):
            score = .5
        result.append({"n": "N" + n.removeprefix("N"), "speaker_pick": pick,
            "level": level, "score": max(0., min(1., score)),
            "evidence": str(row.get("evidence") or "")[:100],
            "to_jiangshi": row.get("to_jiangshi") if row.get("to_jiangshi") in {"yes", "maybe", "no"} else "maybe",
            "address_reason": str(row.get("address_reason") or "")[:100]})
        for key, value in parse_evidence_support(row).items():
            result[-1][key] = value
        if result[-1].get("evidence_relation") == "conflict" and result[-1]["level"] == "确定":
            result[-1]["level"] = "可能"
        elif result[-1].get("evidence_relation") == "insufficient":
            result[-1]["level"] = "不确定"
            result[-1]["speaker_pick"] = "unknown"
    return {"speaker_judgments": tuple(result), "next_jev_note": str(data.get("next_jev_note") or "")[:120]}


def parse_reply_targets(data):
    raw = data.get("reply_targets")
    return tuple(dict.fromkeys(x for x in raw[:64] if isinstance(x, str) and re.fullmatch(r"[PS]\d+", x))) if isinstance(raw, list) else ()


def parse_evidence_support(row, voice=None, allowed=None):
    """Keep physical evidence supplied by the program separate from model semantics."""
    pick = str(row.get("semantic_pick") or "")
    if not (pick in {"unknown", "new"} or re.fullmatch(r"P\d+", pick)):
        pick = ""
    if allowed is not None and pick.startswith("P") and pick not in allowed:
        pick = ""
    relation = row.get("evidence_relation")
    if relation not in {"agree", "voice_only", "semantic_only", "conflict", "insufficient"}:
        relation = ""
    if voice is not None:
        physical = voice.get("pick", "")
        strength = voice.get("strength", "unavailable")
        if strength == "clear" and physical and pick and pick != "unknown" and physical != pick:
            relation = "conflict"
        elif strength == "unavailable" and relation in {"agree", "voice_only", "conflict"}:
            relation = "semantic_only" if pick and pick != "unknown" else "insufficient"
        elif relation == "agree" and (not pick or pick != physical):
            relation = "insufficient"
    return {"semantic_pick": pick, "semantic_reason": str(row.get("semantic_reason") or "")[:100],
            "evidence_relation": relation}
