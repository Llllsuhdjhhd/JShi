"""JEV decides whether an utterance warrants interrupting ongoing playback."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
import re
from time import monotonic

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
    identity_only: bool = False  # Current protocol leaves addressing to cognition.

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
    main_prompt_hint: dict | None = None
    routing_error: str = ""  # Valid identity judgments survive invalid routing fields.


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
        self._unavailable_until = 0.0
        self._unavailable_error = ''

    def decide(self, subject_id: str, text: str, speaker: dict, delivery: dict, *, overlap: bool = False, on_request=None) -> InterruptDecision:
        normalized = text.strip().rstrip("。！!，,？?").strip()
        if normalized in {"停", "停止", "别说了", "停一下", "等等", "等一下"}:
            return InterruptDecision("stop", "explicit stop")
        if re.match(r'^(?:匠石[，,：:\s]*)?(?:停止|别说了|停一下|先停|不要说了)(?:[，,。！!？?\s]|$)', normalized):
            return InterruptDecision('stop', 'explicit stop')
        active = bool(delivery) and delivery.get("state") in {"playing", "paused", "queued"}
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
        if monotonic() < self._unavailable_until:
            return BatchDecision('respond', 'JEV接口暂不可用', self._items_for(batch, 'maybe', '入口判断失败'),
                                 model_error=self._unavailable_error + '；60秒冷却期间不重复请求')
        from .jev_prompts import ENTRY_INSTRUCTION
        thin_batch = []
        for row in batch:
            who = str(row.get("who") or "")
            code = who.split("（", 1)[0]
            thin = {"i": row["n"], "p": code if re.fullmatch(r"P\d+", code) else "unknown", "at": row.get("at", row.get("at_ms")),
                    "text": row["text"], "voice": dict(row.get("voice_evidence") or {})}
            if '临时声音连续性' in str(thin['voice'].get('basis','')):
                thin['identity'] = 'unresolved'
                thin['continuity'] = {**thin['voice'], 'scope':'continuity_only'}
                thin['voice'] = {'pick':'', 'strength':'weak', 'basis':'本句候选比较见candidates；程序未确认实名'}
            if thin["p"] != "unknown" and who != code:
                thin["name"] = who[len(code):].strip("（）")
            if "track" in row:
                thin["track"] = row["track"]
            candidates = [{"p": c["who"], "similarity": round(c["score"], 3) if isinstance(c.get("score"), (int, float)) else None,
                           "source": c.get("source", "voice"), "names": c.get("names", [])}
                          for c in row.get("candidates", [])]
            if candidates:
                thin["candidates"] = candidates
                ranked = sorted((c for c in candidates if isinstance(c['similarity'],(int,float))),key=lambda c:c['similarity'],reverse=True)
                if ranked:
                    thin['voice_ranking'] = {'first':ranked[0]['p'], 'similarity':ranked[0]['similarity']}
                    if len(ranked)>1:
                        thin['voice_ranking'].update(second=ranked[1]['p'],gap=round(ranked[0]['similarity']-ranked[1]['similarity'],3))
                recent = [r for r in context if isinstance(r, dict) and r.get('p') in {c['p'] for c in candidates}
                          and r.get('status') == 'recognized' and isinstance(r.get('at'), (int,float))
                          and isinstance(thin['at'], (int,float)) and 0 <= thin['at'] - r['at'] <= 30000]
                if recent:
                    r = max(recent, key=lambda v:v['at'])
                    thin['recent_confirmed'] = {'p':r['p'], 'gap_ms':round(thin['at']-r['at']),
                        'same_track':bool(r.get('track') and r.get('track') == thin.get('track'))}
            if row.get("short_voice_hint"):
                thin["short_voice_hint"] = "同一短句重复拼接，辅助比较，不是新增证据"
            thin_batch.append(thin)
        payload = {"input": {"state": delivery.get("state", ""), "names": delivery.get("names_for_jiangshi", ["匠石"]),
                             "unwritten": context, "lines": thin_batch},
                   "scene": delivery.get("jev_scene", ""), "now": delivery.get("now")}
        schema = {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "i": {"type": "integer", "enum": [row['n'] for row in batch]}, "person": {"type": "string", "enum": sorted({"unknown", *[
                    c["p"] for line in thin_batch for c in line.get("candidates", []) if c.get('source') != 'temporary_voice'], *[
                    line["p"] for line in thin_batch if line.get('identity') != 'unresolved']})},
                "certainty": {"enum": ["confirmed", "tentative", "unknown"]},
                "why": {"type": "string", "maxLength": 80}},
                "required": ["i", "person", "certainty", "why"], "additionalProperties": False},
                "minItems": len(batch), "maxItems": len(batch)},
            "level": {"type": "integer", "enum": [1, 2, 3]},
            "recall_memory": {"type":"boolean"}},
            "required": ["items", "level", "recall_memory"], "additionalProperties": False}
        request = ModelRequest(purpose="voice_jev", input_text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            subject_state=SubjectState(subject_id, "匠石", "快速入口判断"),
            system_extra=ENTRY_INSTRUCTION + "\nJSON Schema：" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
        raw_text = ""
        try:
            if on_request is not None:
                on_request(request)
            raw_text = self.model.generate(request).text
            data = parse_json_object(raw_text)
            if isinstance(data.get("items"), list):
                from jshi.core.prompt_profile import normalize_hint
                modern = "level" in data or any("i" in row for row in data["items"] if isinstance(row, dict))
                if modern:
                    result = self._from_current(data["items"], batch, rules.claimed_name)
                    hint = normalize_hint({"level": data.get("level"), "recall_memory": data.get("recall_memory")})
                    if not hint:
                        hint = {"level": 1, "needs": [], "recall_memory": False}
                        result = replace(result, routing_error='JEV档位或回忆开关缺失/无效；保留人物终审，回退完整档且不追加历史召回')
                else:
                    result = self._from_items(data["items"], batch, rules.claimed_name, needs_main_review=data.get("needs_main_review") is True)
                control = data.get("control")
                active = delivery.get("state") in {"playing", "paused", "queued"}
                if not modern and control == "stop":
                    # Stop permission needs an explicit instruction, not just a model flag.
                    if any(re.search(r"停止|别说了|停一下|不要说|先停", t) for t in texts):
                        result = replace(result, action="stop", reason="明确停止要求")
                elif not modern and control == "continue" and active and all(r.to_jiangshi == "no" for r in result.items):
                    result = replace(result, action="resume")
                return replace(result, model_raw=raw_text,
                    main_prompt_hint=hint if modern else normalize_hint(data.get("main_prompt_hint")) or None)
            action = data.get("action")
            if action in {"ignore", "resume", "stop", "respond", "clarify"}:
                return replace(self._from_legacy(InterruptDecision(action, str(data.get("reason", ""))[:200], rules.claimed_name), batch), model_raw=raw_text)
            raise ValueError("invalid JEV batch")
        except Exception as exc:
            if getattr(exc, 'code', None) in {401, 402, 403}:
                self._unavailable_until = monotonic() + 60
                self._unavailable_error = f'{type(exc).__name__}: HTTP {exc.code}'
            return BatchDecision("respond", "未能初判", self._items_for(batch, "maybe", "未能初判"), rules.claimed_name, False,
                model_raw=raw_text, model_error=f"{type(exc).__name__}: {exc}")

    def _from_current(self, raw, batch, claimed):
        expected = {int(r["n"]) for r in batch}
        seen, items = set(), []
        for row in raw:
            if not isinstance(row, dict) or type(row.get("i")) is not int or row["i"] not in expected or row["i"] in seen:
                raise ValueError("JEV输出编号重复或不属于本批")
            seen.add(row["i"])
            source = next(r for r in batch if r["n"] == row["i"])
            allowed = {c["who"] for c in source.get("candidates", [])}
            unresolved = '临时声音连续性' in str((source.get('voice_evidence') or {}).get('basis',''))
            if unresolved:
                allowed = {c['who'] for c in source.get('candidates',[]) if c.get('source') != 'temporary_voice'}
            who = str(source.get("who") or "").split("（", 1)[0]
            if re.fullmatch(r"P\d+", who) and not unresolved:
                allowed.add(who)
            pick = row.get("person")
            certainty = row.get("certainty")
            # Accept only exact labels supplied for this row; never guess a name's ID.
            if pick == source.get("who"):
                pick = who if re.fullmatch(r"P\d+", who) else "unknown" if certainty == "unknown" else pick
            if pick not in allowed | {"unknown"} or certainty not in {"confirmed", "tentative", "unknown"}:
                raise ValueError("JEV输出非法人物或确定程度")
            if pick != 'unknown' and certainty == 'unknown':
                raise ValueError('JEV已选人物却标为未知')
            voice = source.get("voice_evidence") or {}
            if not isinstance(row.get('why'), str) or not row['why'].strip() or len(row['why']) > 80:
                raise ValueError('JEV人物依据缺失或超过80字符')
            why = row['why']
            # Contrary final attribution must carry independently checkable grounds.
            if pick != voice.get("pick") and pick != "unknown" and not why:
                raise ValueError("JEV调整人物归属缺少依据")
            if pick == "unknown" and voice.get("strength") == "clear" and not why:
                raise ValueError("JEV反驳明确声纹缺少依据")
            if pick == "unknown":
                certainty = "unknown"
            relation = "voice_only" if pick == voice.get("pick") else "semantic_only" if pick != "unknown" else "insufficient"
            items.append(BatchItem(row["i"], "", reason=why, relevance="", identity_only=True,
                speaker_pick=pick, speaker_level={"confirmed": "确定", "tentative": "可能", "unknown": "不确定"}[certainty],
                speaker_reason=why, voice_pick=voice.get("pick", ""), voice_strength=voice.get("strength", "unavailable"),
                semantic_pick=pick if relation == "semantic_only" else "unknown", semantic_reason=why, evidence_relation=relation))
        if seen != expected:
            raise ValueError("JEV输出遗漏本批发言")
        items = tuple(sorted(items, key=lambda r:r.n))
        return BatchDecision("respond", "人物终审完成，是否回应交主认知", items, claimed, True)

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
