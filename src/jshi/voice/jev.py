"""JEV decides whether an utterance warrants interrupting ongoing playback."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
import re

from jshi.core import SubjectState
from jshi.models import ModelRequest
from jshi.skill.base import parse_json_object


@dataclass(frozen=True)
class InterruptDecision:
    action: str = "respond"  # ignore / resume / stop / respond / clarify
    reason: str = "new utterance"
    claimed_name: str = ""


@dataclass(frozen=True)
class BatchItem:
    """一条发言的初判。to_jiangshi 是对话指向，不是声纹分数。"""

    n: int
    to_jiangshi: str = "maybe"  # yes / maybe / no
    score: float = 0.5
    reason: str = ""
    speaker_pick: str = ""
    speaker_level: str = ""
    speaker_reason: str = ""
    relevance: str = "uncertain"  # related / uncertain / unrelated
    speaker_score: float | None = None
    voice_pick: str = ""
    voice_strength: str = "unavailable"
    semantic_pick: str = ""
    semantic_reason: str = ""
    evidence_relation: str = ""

    @property
    def keep(self) -> bool:
        """不直接对匠石说的话，也可能是理解本轮所需的背景。"""
        return self.to_jiangshi != "no" or self.relevance != "unrelated"


@dataclass(frozen=True)
class BatchDecision:
    action: str = "respond"
    reason: str = ""
    items: tuple[BatchItem, ...] = ()
    claimed_name: str = ""
    judged: bool = False
    needs_main_review: bool = False
    model_raw: str = ""
    model_error: str = ""


def explicit_name(text: str) -> str:
    match = re.match(r"\s*(?:我叫(?!你|您)|我是)\s*([^，。！？!?\n]{1,30})(?:[，。！？!?\n]|$)", text)
    if not match:
        return ""
    name = re.split(r"你好|您好|大家好|我想|我在", match.group(1), maxsplit=1)[0].strip()
    if re.fullmatch(r"[A-Za-z](?:[A-Za-z\s]*[A-Za-z])?", name):
        name = re.sub(r"\s+", "", name)
    return name


class VoiceJEV:
    def __init__(self, model=None) -> None:
        self.model = model

    def decide(self, subject_id: str, text: str, speaker: dict, delivery: dict, *, overlap: bool = False, on_request=None) -> InterruptDecision:
        normalized = text.strip().rstrip("。！!，,？?").strip()
        if normalized in {"停", "停止", "别说了", "停一下", "等等", "等一下"}:
            return InterruptDecision("stop", "explicit stop")
        active = bool(delivery) and delivery.get("state") not in {"completed", "stopped", "failed"}
        if overlap:
            return InterruptDecision("resume" if active else "ignore", "overlapping speech; avoid blind interruption")
        if not normalized:
            return InterruptDecision("resume", "no utterance")
        if normalized in {"了", "就", "那个", "的", "是"}:
            return InterruptDecision("resume", "incomplete fragment; wait for meaningful utterance")
        if normalized in {"嗯", "啊", "哦", "是", "对", "对的", "好", "好的", "那个", "就", "了", "哈哈", "哈哈哈哈"}:
            return InterruptDecision("resume", "backchannel or incomplete fragment; keep playing")
        claimed = explicit_name(text)
        if self.model is None:
            return InterruptDecision("resume" if active else "respond", "JEV rule fallback", claimed)
        request = ModelRequest(
            purpose="voice_jev",
            input_text=json.dumps({"utterance": text, "speaker": speaker, "delivery": delivery}, ensure_ascii=False),
            subject_state=SubjectState(subject_id, "匠石", "判断当前插话如何接续"),
            system_extra=("你是匠石的 JEV，判断这一批现场发言是否在对匠石说话、是否需要回应。按 delivery.state 判断状态，不能假定一直在播放。只输出 JSON："
                '{"action":"ignore|resume|stop|respond|clarify","reason":"简短依据","claimed_name":""}。'
                "空闲或刚说完时，旁人聚会聊天、对别人提问、笑声、附和和没有回应必要的结束语选 ignore：只留现场，不触发主流程回答。"
                "正在播放时判断是否明确插话；正在对话时结合现场判断是否承接对匠石的问题，不因出现问号就认定在问匠石。"
                "resume 表示继续播放，用于附和、噪声、不完整碎片、旁人互聊或并非对匠石说的话；"
                "stop 用于明确要求停止；respond 用于向匠石的提问、补充、纠正、接话、问候、分享或表达感受；"
                "持续对话中的自然承接不要求问号或每句点名匠石，也不要求先提出任务；结合现场判断，不能把旁人互聊当作分享给匠石。"
                "clarify 仅用于明确向匠石说话但需要澄清的内容。换人本身不是打断依据。不要生成回答。"
                "claimed_name 只取说话人明确的自我介绍，不取提及的其他人。"
                "身份未明或换了对象时，不要把附和擅自归给原说话人。"
                "姓名未知、匿名访客、声音归属待定都不是忽略或拒绝进入主流程的理由。"
                "新来者向匠石打招呼、自我介绍、叫匠石名字纠正其名字、询问能否听见，应按内容选 respond 或 clarify。"
                "没有姓名也能正常对话。判断对象是匠石的依据是称呼、语义和上下文，不能要求先登记声纹或确认姓名。"
                "delivery.attention 是接话关注权重：近期对话者更值得关注，但不是身份或声纹证据，"
                "不能仅凭高权重打断，也不能忽略新来者的明确提问。结合 delivery.scene 中各人的发言判断。"
                "人声比普通背景声更值得分析；响亮、尖锐、突出不等于向匠石说话，不据此停止播放。"
                "转写可能包含扬声器回声；如果内容只是复述当前回应，且没有明显向匠石接话，继续播放。"),
        )
        try:
            if on_request is not None:
                on_request(request)
            data = parse_json_object(self.model.generate(request).text)
            action = data.get("action")
            if action not in {"ignore", "resume", "stop", "respond", "clarify"}:
                raise ValueError("invalid JEV action")
            # Bind only explicit self-introductions verified by the program.
            return InterruptDecision(action, str(data.get("reason", ""))[:200], claimed)
        except Exception:
            return InterruptDecision("resume", "JEV unavailable; keep playing", claimed)

    def decide_batch(self, subject_id: str, batch: list, context: list, delivery: dict, *, overlap: bool = False, on_request=None) -> BatchDecision:
        """一次判断整批。旧的单条 action JSON 仍接受，便于已有调用。"""
        texts = [str(item.get("text") or "") for item in batch]
        joined = "\n".join(texts)
        speaker = batch[-1].get("speaker") or {} if batch else {}
        rules = VoiceJEV().decide(subject_id, joined, speaker, delivery, overlap=overlap)
        if rules.action == "stop" and rules.reason == "explicit stop":
            return BatchDecision("stop", rules.reason, self._items_for(batch, "no", "明确停止"), rules.claimed_name, True)
        if self.model is None or overlap or not joined.strip():
            return self._from_legacy(rules, batch)
        payload = {"delivery": {"state": delivery.get("state", ""), "spoken": delivery.get("spoken", ""),
                                "pending": delivery.get("pending", ""), "attention": delivery.get("attention", {})},
                   "context": context, "batch": batch,
                   "person_review": delivery.get("person_review") or [],
                   "interaction_feedback": delivery.get("interaction_feedback") or {},
                   "names_for_jiangshi": delivery.get("names_for_jiangshi") or ["匠石"]}
        request = ModelRequest(
            purpose="voice_jev",
            input_text=json.dumps(payload, ensure_ascii=False),
            subject_state=SubjectState(subject_id, "匠石", "判断这一批发言"),
            system_extra=(
                "你是匠石的 JEV。context 是前文，只用于理解，不评分、不重复回答。"
                "batch 是本批新发言，只给这些评分。前文里匠石的话以已播出为准，标为准备说的内容对方还没听到。"
                "batch 是现场听到的发言，不是都对匠石说的话。分清是谁说和对谁说，‘你’、问句、命令句或已知身份本身都不能证明对话指向。"
                "姓名未知、匿名访客、声音归属待定都不是忽略或拒绝进入主流程的理由。"
                "话里提到的名字只说明在谈论谁，不能说明是谁在说。"
                "明确登记声纹或人工关联的归属受保护；自报姓名只是称呼线索，不等于登记确认。candidates 是候选证据，不能当成已经确认。"
                "speaker.pick 只能选 candidates 里的代号，或 unknown（还不能确定，不表示多了一个人），或 new（更像另一个人，但不建立档案）。"
                "待定声音是正在收集的声音组，不是已确认新人；一次未匹配不能证明出现新人。收集中的声音和临时访客不参与已有姓名的声纹竞争，也不能据此否认对方自报姓名。声音证据不足时可继续正常交流，不能把前几轮同一个猜测累计成新的声纹依据。"
                "voice_evidence是程序给出的物理声音支持，不由你改写。先用原话与前文判断语义支持谁，写speaker.semantic_pick与semantic_reason，再综合声音和语义给speaker.pick、level、score、reason。近期人物候选不是声音证据。"
                "short_voice_hint及source=repeated_short_audio来自同一短句首尾重复拼接的辅助比较；原始时长不变，重复次数不增加独立证据，相似度不是身份概率。可用来比较候选，但单靠它不得确定姓名，不当作clear声纹或多段一致性。"
                "semantic_pick只依据原话与前文中能区分说话人的语义线索；不要把who已有姓名、声纹结论或对话指向拿来证明语义身份。纠正你、承接你的提问说明可能在对你说，但不单独证明是哪位熟人；没有区分身份的语义依据时semantic_pick=unknown，仍可按明确声纹给综合pick，关系写voice_only。"
                "两边支持同一个人时可提高综合可信度；声音弱但语义明确时可主要采信语义；声音明确但语义没有线索时可主要采信声音；两边都弱就unknown。声音与语义指向不同时说明差异，明确冲突尚未解释时不得确定，请求独立人物判断。"
                "speaker.evidence_relation写agree（两边支持同一人）、voice_only（主要靠声音）、semantic_only（主要靠语义）、conflict（未解释的冲突）、insufficient（都不足）。自我介绍属于语义依据，不是声纹确认；上轮评价也不是新增声音证据。"
                "比较所有候选与 unknown/new 后给出最合适的综合pick；speaker.score 是综合可信度参考，不机械相乘，不冒充声纹概率。"
                "to_jiangshi 表示是不是在对匠石说，与声纹分数无关。score 只是参考，不是已校准的概率。"
                "relevance 单独判断这句话对理解本批交往是否有用：related 是相关的背景、条件、补充或纠正；"
                "uncertain 是关系不明确；unrelated 是明确无关。旁人说的话也可能 related，不得仅因 to_jiangshi=no 就判无关。"
                "相关背景与关系不确定的旁人发言应留给主流程理解现场；只有能明确判断无关时才选 unrelated。"
                "保留作背景不等于请求匠石回话，不要为了保留背景而把 to_jiangshi 改成 yes。"
                "候选里的姓名、别名和称呼可以帮助理解；同名人物按不同P代号分开，不因名字相近合并。"
                "names_for_jiangshi是匠石的各种称呼。谁怎么称呼匠石不是声纹证据。"
                "person_review是独立人物判断的建议与短评，不是人物原话或声纹确认。其N编号属于先前那一批，不是当前同名N编号；按basis的input_id、声音连续性和当前原话复核，不能反复引用同一猜测提高确定度。"
                "interaction_feedback记录匠石实际完整播出的问题、等待对象、回答候选及未结束的短评；带来源编号和时间。准备说但未播出的话不是已问。"
                "possible_answer_received只说明可能收到回答，answer_candidate_processed只说明主流程处理过，不证明问题解决，更不证明姓名。等待对象只能帮助理解接续，不作为新的声纹证据。"
                "候选score是质量加权平均相似度，不是概率；reference_count和stddev描述参考数量与波动。只有一段时stddev为空，不视作零波动；参考少仍可比较，证据不足保留未知。"
                "needs_main_review沿用旧字段名，表示请求独立人物判断，不向主认知加材料。只有人物或指向疑问会影响当前或未了结交往时才为true；无关旁人闲聊为false。它不表示必须回话，不通过强写yes来请求复判。"
                "needs_main_review是本批级请求，不是每条的开关；需复核的具体条目在speaker及reason中点明。独立人物判断流程可依据完整上下文提供建议，但不直接修改本轮人物归属或档案；由JEV结合当前新证据作最终输入归属判断，不修改正式档案。n对应独立判断本批N编号，P代号共用人物映射；前文反馈若有S代号只表示声音连续性，不能当作姓名。"
                "无法确定是否对匠石说时选maybe，并写明缺少什么指向依据；maybe只允许主流程理解，不要求开口。"
                "旧任务、旧话题和未开口计划不能单独作为当前对话指向依据。"
                "自然承接匠石实际已播出的话不要求每句点名；旁人之间的承接、提问或纠正仍可选no并保留相关背景。"
                "只输出 JSON："
                '{"needs_main_review":false,"items":[{"n":1,"to_jiangshi":"yes|maybe|no","relevance":"related|uncertain|unrelated","score":0.5,"reason":"简短依据",'
                '"speaker":{"semantic_pick":"P1|unknown|new","semantic_reason":"","evidence_relation":"agree|voice_only|semantic_only|conflict|insufficient","pick":"P1|unknown|new","level":"确定|可能|不太可能|不确定","score":0.5,"reason":"综合依据"}}]}。'
                "不要生成回答。"),
        )
        raw_text = ""
        try:
            if on_request is not None:
                on_request(request)
            raw_text = self.model.generate(request).text
            data = parse_json_object(raw_text)
            if isinstance(data.get("items"), list):
                return replace(self._from_items(data["items"], batch, rules.claimed_name, needs_main_review=data.get("needs_main_review") is True), model_raw=raw_text)
            action = data.get("action")
            if action in {"ignore", "resume", "stop", "respond", "clarify"}:
                return replace(self._from_legacy(InterruptDecision(action, str(data.get("reason", ""))[:200], rules.claimed_name), batch), model_raw=raw_text)
            raise ValueError("invalid JEV batch")
        except Exception as exc:
            return BatchDecision("respond", "未能初判", self._items_for(batch, "maybe", "未能初判"), rules.claimed_name, False,
                model_raw=raw_text, model_error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _items_for(batch, mark: str, reason: str) -> tuple[BatchItem, ...]:
        return tuple(BatchItem(int(item.get("n") or i + 1), mark, 0.5, reason,
                              relevance="unrelated" if mark == "no" else "uncertain") for i, item in enumerate(batch))

    def _from_legacy(self, decision: InterruptDecision, batch: list) -> BatchDecision:
        if decision.action == "stop":
            mark = "no"
        elif decision.action in {"ignore", "resume"}:
            mark = "no"
        else:
            mark = "yes"
        action = {"stop": "stop", "ignore": "ignore", "resume": "resume"}.get(decision.action, "respond")
        return BatchDecision(action, decision.reason, self._items_for(batch, mark, decision.reason), decision.claimed_name, True)

    def _from_items(self, raw, batch: list, claimed: str, *, needs_main_review=False) -> BatchDecision:
        by_n = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("n"))
            except (TypeError, ValueError):
                continue
            mark = item.get("to_jiangshi")
            if mark not in {"yes", "maybe", "no"}:
                mark = "maybe"
            try:
                score = float(item.get("score", 0.5))
            except (TypeError, ValueError):
                score = 0.5
            if not math.isfinite(score):
                score = 0.5
            relevance = item.get("relevance")
            if relevance not in {"related", "uncertain", "unrelated"}:
                relevance = "uncertain"
            speaker = item.get("speaker") if isinstance(item.get("speaker"), dict) else {}
            try:
                speaker_score = float(speaker.get("score"))
                speaker_score = max(0.0, min(1.0, speaker_score)) if math.isfinite(speaker_score) else None
            except (TypeError, ValueError):
                speaker_score = None
            level = speaker.get("level") if speaker.get("level") in {"确定", "可能", "不太可能", "不确定"} else ""
            pick = str(speaker.get("pick") or "")
            if pick not in {"unknown", "new"} and not re.fullmatch(r"P\d+", pick):
                pick = ""
            from jshi.core.conversation_review import parse_evidence_support
            source = next((row for i, row in enumerate(batch, 1) if int(row.get("n") or i) == n), {})
            voice = source.get("voice_evidence") or {}
            support = parse_evidence_support(speaker, voice=voice,
                allowed={row.get("who") for row in source.get("candidates", [])})
            if support["evidence_relation"] == "conflict" and level == "确定":
                level = "可能"
            elif support["evidence_relation"] == "insufficient":
                level, pick = "不确定", "unknown"
            by_n[n] = BatchItem(n, mark, max(0.0, min(1.0, score)), str(item.get("reason", ""))[:80],
                                pick, level, str(speaker.get("reason") or "")[:80], relevance, speaker_score,
                                voice.get("pick", ""), voice.get("strength", "unavailable"), **support)
        items = tuple(by_n.get(int(item.get("n") or i + 1), BatchItem(int(item.get("n") or i + 1))) for i, item in enumerate(batch))
        review = (bool(needs_main_review) or any(item.evidence_relation == "conflict" for item in items)) and any(item.keep for item in items)
        if any(item.to_jiangshi in {"yes", "maybe"} for item in items):
            action, reason = "respond", "至少一条可能在对匠石说"
        elif review:
            action, reason = "respond", "相关人物或指向有疑问，另交独立判断，不要求开口"
        else:
            action, reason = "ignore", "本批没有需要接话的内容"
        return BatchDecision(action, reason, items, claimed, True, review)
