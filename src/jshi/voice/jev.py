"""JEV decides whether an utterance warrants interrupting ongoing playback."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
import os
import re
from time import monotonic

from jshi.core import SubjectState
from jshi.models import ModelRequest
from jshi.models.routing import (
    ProviderFailure,
    ProviderModel,
    RoutedModel,
    _cap_model_timeout,
    _model_name,
    is_lite_model,
)
from jshi.skill.base import parse_json_object
from .jev_prompts import SINGLE_INSTRUCTION

# Wall-clock budget for decide / decide_batch model calls (lite + one Qwen fallback).
JEV_GATE_BUDGET_S = 8.0
# Keep at least this much of budget_s for the single Qwen fallback after a lite timeout.
JEV_GATE_FALLBACK_RESERVE_S = 3.0


def default_jev_fallback_models():
    """Build Bailian/DashScope qwen3.5-flash from existing env plumbing when keyed."""
    api_key = os.getenv('JSHI_API_QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY', '')
    if not api_key:
        return []
    endpoint = (os.getenv('JSHI_API_QWEN_ENDPOINT') or os.getenv('DASHSCOPE_BASE_URL')
                or 'https://dashscope.aliyuncs.com/compatible-mode/v1')
    service = {
        'label': '阿里云百炼 Qwen（可计费）',
        'protocol': 'chat',
        'thinking_style': 'qwen',
        'paid': True,
        'endpoint': endpoint,
        'api_key': api_key,
    }
    # effort='off' → ProviderModel emits enable_thinking=false on the DashScope body.
    return [ProviderModel(service, 'qwen3.5-flash', 'off', timeout=ProviderModel.DEFAULT_TIMEOUT_S)]


def _jev_fallback_provider(model):
    """JEV fallback path only: Qwen must send enable_thinking false (not thinking-on ~14s)."""
    if isinstance(model, ProviderModel) and (model.service or {}).get('thinking_style') == 'qwen':
        if model.effort == 'off':
            return model
        return ProviderModel(model.service, model.name, 'off', model.max_tokens, timeout=model.timeout)
    return model


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
    direct_reply: str = ""
    waiting_reply: str = ""

    @property
    def front_choice(self):
        if self.direct_reply:
            return 'self_reply'
        if self.waiting_reply:
            return f"main_l{(self.main_prompt_hint or {}).get('level', 1)}_ack"
        return 'main_silent'


def explicit_name(text: str) -> str:
    match = re.match(r"\s*(?:我叫(?!你|您)|我是)\s*([^，。！？!?\n]{1,30})(?:[，。！？!?\n]|$)", text)
    if not match:
        return ""
    name = re.split(r"你好|您好|大家好|我想|我在", match.group(1), maxsplit=1)[0].strip()
    if re.fullmatch(r"[A-Za-z](?:[A-Za-z\s]*[A-Za-z])?", name):
        name = re.sub(r"\s+", "", name)
    return name


class VoiceJEV:
    def __init__(self, model=None, *, budget_s: float = JEV_GATE_BUDGET_S,
                 fallback_models=None, fallback_reserve_s: float = JEV_GATE_FALLBACK_RESERVE_S) -> None:
        self.model = model
        self.budget_s = float(budget_s)
        self.fallback_models = (list(fallback_models) if fallback_models is not None
                                else default_jev_fallback_models())
        self.fallback_reserve_s = float(fallback_reserve_s)
        self._unavailable_until = 0.0
        self._unavailable_error = ''

    def _generate_gate(self, request):
        """Call the gate model within ``budget_s``; lite timeout → one ``fallback_models`` try.

        Never retries lite. Lite per-call timeout is capped at
        ``min(timeout, budget remaining − fallback_reserve_s)`` so Qwen keeps a reserve
        (default 3s; ≈5s lite with budget 8). If the budget is exhausted or the single
        fallback fails, raise so callers use ``_unavailable_hold`` (never respond).
        """
        deadline = monotonic() + self.budget_s

        def remaining():
            return deadline - monotonic()

        def call_one(model, *, reserve=0.0):
            left = remaining()
            usable = left - float(reserve)
            if usable <= 0:
                raise ProviderFailure(408, True, 'JEV gate', 'wall-clock budget exhausted')
            return _cap_model_timeout(model, usable).generate(request)

        def lite_reserve():
            # Only reserve when a fallback provider is configured.
            return self.fallback_reserve_s if self.fallback_models else 0.0

        def try_fallback(from_exc):
            if remaining() <= 0:
                raise ProviderFailure(408, True, 'JEV gate', 'wall-clock budget exhausted') from from_exc
            if not self.fallback_models:
                raise from_exc
            # Exactly one call to the first fallback provider (Qwen by default); no reserve.
            # JEV-only: force DashScope enable_thinking false (thinking on ≈14s; off ≈1–3s).
            return call_one(_jev_fallback_provider(self.fallback_models[0]), reserve=0.0)

        primary = self.model
        if isinstance(primary, RoutedModel):
            chain = primary._snapshot()
            failed = []
            for index, model in enumerate(chain):
                try:
                    with primary._observe(model):
                        primary._notify_paid(model)
                        reserve = lite_reserve() if is_lite_model(model) else 0.0
                        result = call_one(model, reserve=reserve)
                    reason = failed[0]['reason'] if failed else ''
                    primary._note(model, index, degraded=bool(failed), fallback_reason=reason, failed=failed)
                    return result
                except Exception as exc:
                    failed.append({'model': _model_name(model), 'reason': RoutedModel._fallback_reason(exc)})
                    timed_out = RoutedModel._is_timeout(exc)
                    if timed_out and is_lite_model(model):
                        # Do not retry lite or continue the primary chain past it.
                        primary._note(model, index, degraded=True, fallback_reason='timeout', failed=failed)
                        result = try_fallback(exc)
                        fb = self.fallback_models[0]
                        primary._note(fb, index + 1, degraded=True, fallback_reason='timeout', failed=failed)
                        return result
                    if index + 1 == len(chain) or not primary._retryable(exc) or remaining() <= 0:
                        if remaining() <= 0:
                            primary._note(model, index, degraded=bool(failed), fallback_reason='timeout', failed=failed)
                            raise ProviderFailure(408, True, 'JEV gate', 'wall-clock budget exhausted') from exc
                        primary._note(model, index, degraded=bool(failed),
                                      fallback_reason=RoutedModel._fallback_reason(exc), failed=failed)
                        raise
            raise ProviderFailure(408, True, 'JEV gate', 'wall-clock budget exhausted')

        try:
            reserve = lite_reserve() if is_lite_model(primary) else 0.0
            return call_one(primary, reserve=reserve)
        except Exception as exc:
            # Lite timeout only: do not retry lite; one fallback_models call with remaining budget.
            if RoutedModel._is_timeout(exc) and is_lite_model(primary):
                return try_fallback(exc)
            raise

    def decide_envelope(self, subject_id, envelope, delivery, *, vision=None, on_request=None):
        from jshi.core.inputpack import packet
        delivery = {**delivery, 'envelope': packet(envelope, vision, subject_id)}
        lines = envelope.current_utterances
        batch = [{'n': i, 'text': u.text, 'at': u.received_at_ms,
                  'who': 'P1' if u.speaker.method == 'user_selected' else 'unknown', 'source': envelope.source, 'speaker': {'method': u.speaker.method},
                  'voice_evidence': {'strength': 'unavailable', 'basis': u.identity_note}}
                 for i, u in enumerate(lines, 1)]
        if not batch:
            batch = [{'n': 1, 'text': envelope.text, 'who': 'P1' if envelope.speaker.method == 'user_selected' else 'unknown', 'at': delivery.get('now'),
                      'source': envelope.source, 'speaker': {'method': envelope.speaker.method},
                      'voice_evidence': {'strength': 'unavailable', 'basis': '非声音输入，无声纹证据'}}]
        return self.decide_batch(subject_id, batch, delivery.get('unwritten', []), delivery, overlap=envelope.overlap, on_request=on_request)

    def decide(self, subject_id: str, text: str, speaker: dict, delivery: dict, *, overlap: bool = False, on_request=None) -> InterruptDecision:
        normalized = text.strip().rstrip("。！!，,？?").strip()
        if re.match(r'^(?:匠石[，,：:\s]*)?(?:(?:好[了啦]?){1,3}[，,\s]*)?(?:先[，,\s]*){0,2}(?:别说[了啦]|不要说[了啦]|停止|停一下|先停)(?=[，,。！!？?\s]|$|别说[了啦]|不要说[了啦]|停止|停一下)', normalized):
            return InterruptDecision('stop', 'explicit stop')
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
        from .jev_input import compact_jev_input
        # Retain conversational context, not the full host envelope and records.
        context = {k:delivery[k] for k in ('state', 'spoken', 'pending', 'names_for_jiangshi',
            'attention', 'scene', 'main_scene', 'person_portraits', 'collection') if k in delivery}
        payload = compact_jev_input({'utterance':text, 'speaker':speaker,
                                     'delivery':context, 'now':delivery.get('now')})
        request = ModelRequest(
            purpose="voice_jev",
            input_text=json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
            subject_state=SubjectState(subject_id, "匠石", "判断当前插话如何接续"),
            system_extra=SINGLE_INSTRUCTION,
        )
        try:
            if on_request is not None:
                on_request(request)
            data = parse_json_object(self._generate_gate(request).text)
            action = data.get("action")
            if action not in {"ignore", "resume", "stop", "respond", "clarify"}:
                raise ValueError("invalid JEV action")
            # Bind only explicit self-introductions verified by the program.
            return InterruptDecision(action, str(data.get("reason", ""))[:200], claimed)
        except Exception:
            # Unavailable must not fail-open into respond (false_respond on garbage/ASR noise).
            action, reason = self._unavailable_hold(delivery)
            return InterruptDecision(action, reason, claimed)

    def decide_batch(self, subject_id: str, batch: list, context: list, delivery: dict, *, overlap: bool = False, on_request=None) -> BatchDecision:
        """一次判断整批。旧的单条 action JSON 仍接受，便于已有调用。"""
        texts = [str(item.get("text") or "") for item in batch]
        joined = "\n".join(texts)
        speaker = batch[-1].get("speaker") or {} if batch else {}
        rules = VoiceJEV().decide(subject_id, joined, speaker, delivery, overlap=overlap)
        if batch and all(row.get('source') in {'web_text', 'visual_environment'} for row in batch) and rules.action != 'stop':
            rules = InterruptDecision('respond', '已封装的文字或观察输入', rules.claimed_name)
        if rules.action == "stop" and rules.reason == "explicit stop":
            return BatchDecision("stop", rules.reason, self._items_for(batch, "no", "明确停止"), rules.claimed_name, True)
        if overlap:
            # Overlap weakens identity evidence; it does not make speech unrelated.
            batch = [{**row, 'who': 'unknown', 'candidates': [], 'overlap': True,
                      'voice_evidence': {'strength': 'unavailable', 'basis': '重叠音频，不能确认人物'}}
                     if row.get('overlap', True) else row for row in batch]
            if self.model is None:
                return BatchDecision('respond', '重叠发言保留给主认知',
                    tuple(BatchItem(row['n'], 'maybe', reason='重叠音频，身份未明', identity_only=True) for row in batch))
        if self.model is None or not joined.strip():
            return self._from_legacy(rules, batch)
        if monotonic() < self._unavailable_until:
            action, reason = self._unavailable_hold(delivery, batch_reason='JEV接口暂不可用')
            return BatchDecision(action, reason, self._items_for(batch, 'no', '入口判断失败'),
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
            # Weak / unresolved / unidentified: attach same-track cluster continuity
            # so JEV can keep one speaker without inventing a new recognized person.
            voice_strength = str((thin.get('voice') or {}).get('strength')
                                 or (row.get('voice_evidence') or {}).get('strength') or '')
            needs_cluster = (thin.get('identity') == 'unresolved'
                             or voice_strength in {'weak', 'unavailable'})
            if needs_cluster and thin.get('track') and isinstance(thin.get('at'), (int, float)):
                same_cluster = [r for r in context if isinstance(r, dict)
                                and r.get('track') and r.get('track') == thin['track']
                                and isinstance(r.get('at'), (int, float))
                                and 0 <= thin['at'] - r['at'] <= 30000]
                if same_cluster:
                    prior = max(same_cluster, key=lambda v: v['at'])
                    cont = dict(thin.get('continuity') or {'scope': 'continuity_only'})
                    cont.update(same_cluster=True, prior_p=prior.get('p'),
                                gap_ms=round(thin['at'] - prior['at']))
                    judgment = prior.get('judgment') if isinstance(prior.get('judgment'), dict) else {}
                    if judgment.get('person') and judgment['person'] not in {'', 'unknown', 'new'}:
                        cont['prior_judgment'] = judgment['person']
                    thin['continuity'] = cont
            if row.get("short_voice_hint"):
                thin["short_voice_hint"] = "同一短句重复拼接，辅助比较，不是新增证据"
            if row.get('source'):
                thin['source'] = row['source']
            if row.get('overlap'):
                thin['overlap'] = True
            thin_batch.append(thin)
        payload = {"input": {"state": delivery.get("state", ""), "names": delivery.get("names_for_jiangshi", ["匠石"]),
                             "unwritten": context, "lines": thin_batch},
                   "scene": delivery.get("jev_scene", ""), "now": delivery.get("now")}
        if delivery.get('environment'):
            payload['environment'] = delivery['environment']
        payload['short_chat'] = delivery.get('short_chat', {'allowed':False})
        payload['waiting'] = delivery.get('waiting', {'allowed':False})
        packed = delivery.get('envelope')
        if packed:
            payload['input']['envelope'] = {**packed, 'parts':[
                {k:v for k,v in p.items() if k != 'image_url'} for p in packed.get('parts', ())]}
        from .jev_input import compact_jev_input
        payload = compact_jev_input(payload)
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
            "recall_memory": {"type":"boolean"},
            "main_image": {"enum":["omit", "include"]},
            "route": {"enum":["main", "self_reply"] if delivery.get('short_chat', {}).get('allowed') is True else ["main"]},
            "reply": {"type":"string", "maxLength":160 if delivery.get('short_chat', {}).get('allowed') is True else 0},
            "waiting_reply": {"type":"string", "maxLength":16}},
            "required": ["items", "level", "recall_memory", "main_image", "route", "reply"], "additionalProperties": False}
        request = ModelRequest(purpose="voice_jev", input_text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            subject_state=SubjectState(subject_id, "匠石", "快速入口判断"),
            system_extra=ENTRY_INSTRUCTION + "\nJSON Schema：" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            input_parts=tuple(packed.get('parts', ())) if packed else ())
        raw_text = ""
        try:
            if on_request is not None:
                on_request(request)
            raw_text = self._generate_gate(request).text
            data = parse_json_object(raw_text)
            if isinstance(data.get("items"), list):
                from jshi.core.prompt_profile import normalize_hint
                modern = "level" in data or any("i" in row for row in data["items"] if isinstance(row, dict))
                if modern:
                    result = self._from_current(data["items"], batch, rules.claimed_name)
                    hint = normalize_hint({"level": data.get("level"), "recall_memory": data.get("recall_memory"),
                                           "main_image": data.get("main_image", "omit")})
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
                direct = data.get('reply', '')
                if modern and data.get('route') == 'self_reply' and delivery.get('short_chat', {}).get('allowed') is not True:
                    result = replace(result, routing_error='本轮没有直接回答许可；保留人物终审，交主认知')
                if (modern and data.get('route') == 'self_reply' and delivery.get('short_chat', {}).get('allowed') is True
                        and not overlap and not any(row.get('source') == 'visual_environment' for row in batch)
                        and hint.get('level') == 3 and hint.get('recall_memory') is False
                        and hint.get('main_image', 'omit') == 'omit'
                        and result.judged and not result.routing_error
                        and isinstance(direct, str) and 0 < len(direct.strip()) <= 160
                        and all(row.speaker_pick == delivery['short_chat'].get('person_code', row.speaker_pick)
                                for row in result.items)
                        and all(row.speaker_level == '确定' for row in result.items)):
                    result = replace(result, direct_reply=direct.strip(), reason='短时聊天，由JEV我回答')
                waiting = delivery.get('waiting', {})
                brief = data.get('waiting_reply', '')
                budget = waiting.get('levels', {}).get(f"L{hint.get('level')}", {}) if modern else {}
                if (modern and data.get('route') == 'main' and waiting.get('allowed') is True
                        and not overlap and not result.routing_error and result.judged
                        and not any(row.get('source') == 'visual_environment' for row in batch)
                        and budget.get('expected_seconds', 0) >= 2
                        and isinstance(brief, str) and 0 < len(brief.strip()) <= min(16, budget.get('max_chars', 0))):
                    result = replace(result, waiting_reply=brief.strip())
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
            action, reason = self._unavailable_hold(delivery, batch_reason='未能初判')
            return BatchDecision(action, reason, self._items_for(batch, "no", "未能初判"), rules.claimed_name, False,
                model_raw=raw_text, model_error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _unavailable_hold(delivery: dict | None, *, batch_reason: str = '') -> tuple[str, str]:
        """When JEV cannot judge, prefer no-answer over false respond.

        Keep playing only when playback is clearly in progress; otherwise wait/ignore.
        """
        active = bool(delivery) and delivery.get("state") in {"playing", "paused", "queued"}
        if active:
            return "resume", batch_reason or "JEV unavailable; keep playing"
        return "ignore", batch_reason or "JEV unavailable; wait"

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
