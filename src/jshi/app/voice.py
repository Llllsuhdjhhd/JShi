"""Local microphone UI; speech credentials never leave the Python server."""
from __future__ import annotations

import asyncio
import hashlib
import base64
import json
import logging
from time import monotonic, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4
from collections import deque

from jshi.core.envelope import InputEnvelope, InputPart, SceneUtterance, SpeakerEvidence
from jshi.core.inputpack import pack_input, packet
from jshi.models import ResponseItem, ResponsePlan
from jshi.recognition import ObjectProfile, new_object_id
from jshi.subject.domain import HistoryKind, HistoryRecord
from jshi.voice.config import VoiceConfig
from jshi.voice.attention import ConversationAttention
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.delivery import DeliveryTracker
from jshi.voice.identity import VoiceIdentities, attach_voiceprint
from jshi.voice.interaction import InteractionFeedback
from jshi.voice.jev import BatchDecision, InterruptDecision, VoiceJEV
from jshi.voice.volc import Transcript, TranscriptAssembler, VolcVoice, pack_asr, unpack_asr


class VoiceConversation:
    def __init__(self, process, subject_id: str, cloud, send, *, jev=None, timeout_s=3.0, local_speakers=None, input_pause_s=2.0, sample_source=None, candidate_timeout_s=1.2, session_id=None, input_max_batch_s=6.0, scene_idle_s=15.0, scene_batch_inputs=6, scene_batch_chars=800, scene_max_wait_s=30.0) -> None:
        self.process, self.subject_id, self.cloud, self.send = process, subject_id, cloud, send
        self.session_id = session_id or uuid4().hex
        if not isinstance(self.session_id, str) or not self.session_id.isalnum() or len(self.session_id) > 64:
            raise ValueError("invalid voice session identifier")
        self.identities = VoiceIdentities(process.profiles, subject_id, self.session_id, process.repository)
        self.delivery = DeliveryTracker()
        previous = [r for r in process.repository.list_history(subject_id, HistoryKind.FACT, 100)
                    if r.event_type == "voice_delivery" and r.content.get("reply_id")]
        if previous:
            self.delivery.previous = dict(previous[-1].content)
        self.prior_session_spoken = (self.delivery.previous or {}).get("played_text") or ""
        self.jev = jev or VoiceJEV()
        self.local_speakers = local_speakers
        self.sample_source = sample_source
        self.input_records = {}
        self.sample_bytes = 0
        self.annotations = {}
        self.background_tasks = set()
        self.debug_history = deque(maxlen=12)
        self.voice_timings = {}
        self.last_jev = None
        self.timeout_s = timeout_s
        self.input_max_batch_s = max(.01, float(input_max_batch_s))
        self._last_jev_notice = 0.0
        self.input_pause_s = input_pause_s
        self.last_input_at = 0.0
        self.input_idle = asyncio.Event()
        self.input_idle.set()
        self.queue = asyncio.Queue(maxsize=64)
        self.turn_queue = asyncio.Queue(maxsize=64)
        self.epoch = 0
        self.closed = False
        self.output_disconnected = False
        self.tts_task = None
        self.pending_voice = False
        self.last_track = ""
        self.reply_records = {}
        self.reply_epochs = {}
        self.tool_handling = {}
        self.turn_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jshi-voice-turn")
        self.jev_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jshi-voice-jev")
        self.write_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jshi-voice-write")
        self.code_owner = {}
        self.code_labels = {}
        self.candidate_cache = {}
        self.candidate_timeout_s = max(.01, float(candidate_timeout_s))
        self.candidate_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jshi-voice-candidates")
        self.candidate_future = None
        self.sample_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jshi-voice-samples')
        self.sample_jobs = set()
        self.sound_owner = {}
        self.sound_source = {}
        self.source_speakers = {}
        self.main_reviews = deque(maxlen=2)
        self.jev_scene_calls = deque(maxlen=24)
        self.interaction_feedback = InteractionFeedback()
        self.reply_input_ids = {}
        from jshi.voice.jev_scene import JEVSceneStore, JEVSceneWriter
        self.jev_scene_store = JEVSceneStore(process.repository.path.parent / "jev_scenes" / hashlib.sha256(subject_id.encode()).hexdigest()[:16] / (self.session_id + ".json"))
        saved = self.jev_scene_store.snapshot()
        self.code_owner.update(saved["codes"])
        self.code_labels.update(saved["labels"])
        review_model = getattr(process, 'person_review_model', None) or getattr(self.jev, 'model', None)
        self.person_reviewer = JEVSceneWriter(review_model) if review_model is not None else None
        self.person_review_queue = asyncio.Queue(maxsize=1)
        self._last_main_scene_trigger = monotonic()
        self._last_scene_attempt = 0.0
        self.scene_idle_s = max(0.0, scene_idle_s)
        self.scene_batch_inputs = max(1, scene_batch_inputs)
        self.scene_batch_chars = max(1, scene_batch_chars)
        self.scene_max_wait_s = max(0.0, scene_max_wait_s)
        self.person_review_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jshi-person-review')
        self.person_review_worker = asyncio.create_task(self._person_reviews())
        self.unknown_inputs = process.unknown_inputs
        self.write_queue = asyncio.Queue()
        self.failed_write_jobs = []
        self.write_retried = asyncio.Event()
        self.played_history = deque(maxlen=24)
        self.write_worker = asyncio.create_task(self._writes())
        self.jev_future = None
        self.active_turn = None
        self.scene = deque(maxlen=24)
        self.pending_plans = deque(maxlen=32)
        self.turn_sequence = 0
        self.last_debug = None
        self.scene_id = new_object_id()
        self.attention = ConversationAttention()
        self.remembered_voices = set()
        self.pending_rename = None
        self.unreported_jev = []
        self.worker = asyncio.create_task(self._work())
        self.turn_worker = asyncio.create_task(self._turns())

    async def emit(self, kind: str, **payload) -> None:
        if not self.closed and not self.output_disconnected:
            try:
                await self.send({"type": kind, **payload})
            except ConnectionError:
                # The close handler still owns task/executor cleanup. Suppress
                # further browser writes during that close-handler race.
                self.output_disconnected = True

    async def diagnostics(self, section: str, *, activity_id="", part="all", purpose="all") -> None:
        if section not in {"prompt", "timing", "tool", "input", "scene", "identity", "delivery", "jev", "jev_scene", "response"}:
            return
        if section == "jev_scene":
            await self.emit("debug", section=section, activity_id=activity_id,
                text=json.dumps(self.jev_scene_store.snapshot(), ensure_ascii=False, indent=2))
            return
        snapshot = next((r for r in self.debug_history if r['activity_id'] == activity_id), None) if activity_id else self.last_debug
        if snapshot is None:
            await self.emit("debug", section=section, text="还没有已完成的语音轮次。")
            return
        import json
        activity_id = snapshot["activity_id"]
        if section == "prompt":
            def read_prompt():
                store = getattr(self.process, "step_inputs", None)
                if store is None:
                    return "本轮没有提示词记录。", []
                rows = store._read()
                prompts = {r.get("hash"): r.get("text", "") for r in rows if r.get("kind") == "prompt"}
                jev_calls = snapshot.get('timing', {}).get('voice', {}).get('jev_calls', [])
                linked_ids = {activity_id, *(call['prompt_activity_id'] for call in jev_calls if call.get('prompt_activity_id'))}
                if purpose == "voice_jev_scene":
                    linked_ids.update(r.get("activity_id") for r in rows if r.get("purpose") == "voice_jev_scene"
                        and r.get("activity_id") in self.jev_scene_calls)
                calls = [r for r in rows if r.get("kind") == "call" and r.get("activity_id") in linked_ids
                         and r.get("subject_id") == self.subject_id]
                import re
                from jshi.app.talk_session import _extract_prompt_section, _extract_json_schema, _PROMPT_SECTION_ALIASES
                sections = []
                rendered = []
                for r in calls:
                    system, user = prompts.get(r.get('system_hash'), '记录已清理'), r.get('user_text', '')
                    sections.extend(re.findall(r'^【([^\n】]+)】\s*$', system + '\n' + user, re.M))
                    if purpose != 'all' and r.get('purpose') != purpose:
                        continue
                    if part == 'all': body = 'system\n' + system + '\nuser\n' + user
                    elif part == 'system': body = system
                    elif part == 'user': body = user
                    elif part == 'schema': body = _extract_json_schema(system) or '没有找到 JSON Schema。'
                    else:
                        name = _PROMPT_SECTION_ALIASES.get(part, part)
                        body = _extract_prompt_section(system, name) or _extract_prompt_section(user, name) or '没有找到区块：' + part
                    rendered.append(f"[{r.get('purpose')} · {r.get('model')}]\n{body}")
                empty = '本轮没有该调用的提示词记录。'
                if purpose == 'voice_jev' and not rendered:
                    if jev_calls and all(call.get('prompt_requested') is False for call in jev_calls):
                        empty = '本轮JEV未发出模型请求（规则处理或前次请求仍在运行），没有模型提示词。'
                    else:
                        empty = '这一轮没有可读取的JEV实际提示词（未保存或记录已清理），无法事后还原；新版会记录后续JEV模型请求。'
                return '\n\n'.join(rendered) or empty, list(dict.fromkeys(sections))
            value, sections = await asyncio.to_thread(read_prompt)
        else:
            value = json.dumps(snapshot[section], ensure_ascii=False, indent=2, default=str)
            sections = []
        await self.emit("debug", section=section, activity_id=activity_id, text=value, sections=sections)

    async def enroll_inputs(self, input_ids, name):
        try:
            if not isinstance(input_ids, list) or not 1 <= len(input_ids) <= 32:
                raise ValueError('请选择 1–32 条输入')
            name = str(name).strip()
            if not name or len(name) > 40 or any(c in name for c in '\r\n'):
                raise ValueError('请填写一个简短姓名')
            if self.local_speakers is None:
                raise ValueError('本地声纹模型尚未加载')
            selected_pairs = []
            for iid in dict.fromkeys(input_ids):
                row = self.input_records.get(str(iid))
                if row is None or not row['pcm']:
                    raise ValueError('所选声音已过期或没有音频，请选择近期发言')
                if row['transcript'].overlap:
                    raise ValueError('所选发言含重叠声音，请选择清晰、单人发言')
                selected_pairs.append((iid, row))
            selected_pairs.sort(key=lambda pair: (pair[1]['transcript'].start_ms, pair[1]['transcript'].end_ms))
            selected = [row for _, row in selected_pairs]
            previous = {}
            for iid, row in selected_pairs:
                t = row['transcript']
                old = self.source_speakers.get(iid)
                if old is None:
                    old = self.identities.resolve(t.track_id, voiceprint_id=t.voiceprint_id,
                        cluster_id=t.speaker_cluster_id, uncertain=t.identity_uncertain)
                previous[iid] = old
            evidence, _ = self.identities.introduce('manual-' + uuid4().hex, name, basis='manual_selection')
            if evidence.status != 'introduced':
                raise ValueError('这个姓名对应多个对象，请使用唯一的姓名或别名')
            stats = await asyncio.to_thread(self.local_speakers.enroll_samples, [r['pcm'] for r in selected], evidence.object_id, user_labeled=True,
                                          sample_ids=[iid for iid, _ in selected_pairs], session_id=self.session_id)
            retired = set()
            for iid, row in selected_pairs:
                old = previous[iid]
                profile = self.process.profiles.get(old.object_id)
                if profile and profile.source == 'voice_anonymous' and profile.label.startswith('未命名访客') and old.object_id != evidence.object_id:
                    self.identities.associations[old.object_id] = evidence
                    retired.add(old.object_id)
                cluster = row['transcript'].speaker_cluster_id
                if cluster:
                    self.identities.clusters[cluster] = evidence
                    if hasattr(self.local_speakers, 'discard_pending'):
                        await asyncio.to_thread(self.local_speakers.discard_pending, cluster)
            for old_id in retired:
                await asyncio.to_thread(self.local_speakers.forget, old_id)
            for iid, row in selected_pairs:
                who = replace(evidence, track_id=row['transcript'].track_id, status='recognized', method='manual_annotation')
                self.annotations[iid] = who
            self.scene = deque((replace(u, speaker=self.identities.associated(self.annotations.get(u.input_id, u.speaker))) for u in self.scene), maxlen=24)
            self.record('voice_manual_enrollment', {'input_ids': input_ids, 'object_id': evidence.object_id, 'label': evidence.label,
                'retired_temporary_voice_ids': sorted(retired), **stats})
            self.jev_scene_store.correct({"basis": "manual_annotation", "p": self.assign_code(evidence.object_id, evidence.label),
                "text": "用户将选择的声音明确关联为" + evidence.label, "at": time()*1000},
                codes=self.code_owner, labels=self.code_labels)
            self.unknown_inputs.bind(tuple(input_ids), evidence.object_id)
            await self.emit('enrollment', input_ids=input_ids, object_id=evidence.object_id, label=evidence.label, **stats)
        except Exception as exc:
            ids = list(dict.fromkeys(input_ids)) if isinstance(input_ids, list) else []
            problem_ids = [ids[i] for i in getattr(exc, 'sample_indices', ()) if i < len(ids)]
            await self.emit('enrollment_error', text=str(exc), problem_input_ids=problem_ids)

    def current_speaker(self):
        return next((u.speaker for u in reversed(self.scene)
                     if u.speaker.object_id and u.speaker.method != 'unassigned_audio'), None)

    async def typed(self, text, object_id='') -> None:
        """Text from the voice page speaks as the current person, and can correct a name."""
        text = str(text or '').strip()
        if not text or len(text) > 2000 or any(ord(c) < 32 and c not in '\n\t' for c in text):
            await self.emit('typed_error' if object_id else 'notice', text='请输入 1–2000 个字。')
            return
        if object_id:
            profile = self.process.profiles.get(object_id)
            if profile is None or profile.status == 'rejected' or profile.source == 'voice_scene':
                await self.emit('typed_error', text='所选人物不存在，请刷新人物列表。')
                return
            at = time()*1000
            iid = uuid4().hex
            start = max((u.end_ms for u in self.scene), default=0)
            if self.sample_source is not None:
                with self.sample_source.lock:
                    start = max(start, (self.sample_source.offset+len(self.sample_source.data))//32)
            evidence = SpeakerEvidence('typed:'+object_id, object_id, profile.label, 'recognized', 'user_selected', 1.0)
            code = self.assign_code(object_id, profile.label)
            utterance = SceneUtterance(text, evidence, start, start, input_id=iid, received_at_ms=at,
                                       identity_note='文字输入；人物由用户选择，不是声纹确认')
            self.scene.append(utterance)
            self.input_records[iid] = {'pcm': None, 'received_at_ms': at, 'at': monotonic()}
            while len(self.input_records) > 128:
                old = next(iter(self.input_records))
                self.sample_bytes -= len(self.input_records.pop(old).get('pcm') or b'')
            self.record('web_text_input', {'input_id': iid, 'text': text, 'speaker': asdict(evidence), 'at': at})
            self.jev_scene_store.append(iid, {'p': code, 'text': text,
                'at': at, 'basis': '文字输入，人工选择人物'})
            await self.emit('typed_input', text=text, label=profile.label, input_id=iid)
            self.enqueue_turn(InputEnvelope(session_id=self.session_id, source='web_text',
                parts=(InputPart('text', text),), speaker=evidence, input_id=iid,
                start_ms=start, end_ms=start,
                utterances=tuple(self.scene), current_utterances=(utterance,), deferred_interrupt=True))
            return
        speaker = self.current_speaker()
        if speaker is None:
            await self.emit('notice', text='还没有当前说话人。请先说一句话，或到登记页修改姓名。')
            return
        from jshi.voice.identity import INTERNAL_PREFIX, name_correction
        profile = self.process.profiles.get(speaker.object_id)
        current = profile.label if profile else speaker.label
        anonymous = bool(profile and profile.source == 'voice_anonymous' and current.startswith(INTERNAL_PREFIX))
        corrected = name_correction(text, current, anonymous=anonymous)
        if corrected:
            self.pending_rename = (speaker.object_id, current, corrected)
            await self.emit('rename_prompt', object_id=speaker.object_id, previous=current, name=corrected,
                text=f'要把「{current}」改成「{corrected}」吗？人物编号、声纹和已有对话保持关联。')
            return
        cluster = next((c for c, e in self.identities.clusters.items() if e.object_id == speaker.object_id), '')
        start = 10**12
        await self.accept(Transcript(text, speaker.track_id, start, start + 1, True,
            speaker_cluster_id=cluster, identity_tentative=speaker.method == 'voice_continuity', identity_note='文字输入，没有对应音频'))

    async def apply_rename(self, object_id, name, accept: bool) -> None:
        pending = self.pending_rename
        self.pending_rename = None
        if not accept:
            await self.emit('notice', text='已取消改名。')
            return
        if not pending or pending[0] != object_id or pending[2] != name:
            await self.emit('notice', text='这次改名已经过期，请再发一次。')
            return
        try:
            evidence = self.identities.rename(object_id, name, pending[1], basis='typed_correction')
        except (ValueError, KeyError) as exc:
            await self.emit('notice', text=str(exc))
            return
        if self.local_speakers is not None:
            await asyncio.to_thread(self.local_speakers.confirm, object_id)
        self.scene = deque((replace(u, speaker=self.identities.associated(u.speaker)) for u in self.scene), maxlen=24)
        detail = f'已按文字把「{pending[1]}」改为「{evidence.label}」。人物编号、声纹和对话记录仍属于此人。'
        if pending[1].startswith('未命名访客'):
            detail += f'「{pending[1]}」保留为内部名。'
        await self.emit('identity', **asdict(evidence), detail=detail)
        await self.emit('speaker', **asdict(evidence))
        await self.emit('renamed', object_id=object_id, previous=pending[1], label=evidence.label)

    def record(self, event_type: str, payload: dict) -> None:
        self.process.repository.add_history(HistoryRecord(
            subject_id=self.subject_id, kind=HistoryKind.FACT, event_type=event_type,
            content={"session_id": self.session_id, **payload}, source_ids=()))

    async def pause(self) -> None:
        self.pending_voice = True
        if self.delivery.pause():
            await self.emit("pause", reply_id=self.delivery.snapshot()["reply_id"])

    async def noise(self) -> None:
        self.pending_voice = False
        if self.delivery.resume():
            await self.emit("resume", reply_id=self.delivery.snapshot()["reply_id"])

    async def stop(self, reason="explicit stop") -> None:
        self.epoch += 1
        rid = self.delivery.stop(reason)
        if self.tts_task and not self.tts_task.done():
            self.tts_task.cancel()
        self.pending_plans.clear()
        if reason in {'explicit stop', 'connection closed'}:
            while not self.turn_queue.empty():
                self.turn_queue.get_nowait()
                self.turn_queue.task_done()
        await self.emit("stop", reply_id=rid)
        self.record("voice_delivery", self.delivery.snapshot())

        snapshot = self.delivery.snapshot()
        if rid and snapshot.get('state') == 'stopped':
            self.jev_scene_store.append(f"stopped:{rid}", {"p": "匠石", "at": time()*1000,
                "basis": "播放中断", "text": "播放已中断；曾开始的句子未确认完整播出，未播放内容不算已说。"})
            self.process.note_scene_delivery(self.subject_id, f"stopped:{rid}",
                "（播放器交付中断）" + json.dumps(snapshot, ensure_ascii=False)
                + "；partial_text只表示该句曾开始播放，不知道具体播到哪个字；pending_text尚未播放。")

    async def accept(self, transcript) -> None:
        self.last_input_at = asyncio.get_running_loop().time()
        await self.emit("transcript", **asdict(transcript))
        if not transcript.final:
            if not transcript.track_id.startswith("pending-"):
                evidence = self.identities.resolve(transcript.track_id, voiceprint_id=transcript.voiceprint_id, confidence=transcript.confidence, cluster_id=transcript.speaker_cluster_id, uncertain=transcript.identity_uncertain, tentative=transcript.identity_tentative)
                await self.emit("speaker", **asdict(evidence), jev_status="pending")
            return
        # Bound queue growth without discarding an already accepted input.
        if self.queue.full():
            await self.emit("notice", text="待处理的发言较多，请稍候。")
            return
        iid = uuid4().hex
        transcript = replace(transcript, input_id=iid)
        pcm = self.sample_source.segment(transcript.start_ms, transcript.end_ms) if self.sample_source else None
        if pcm and len(pcm) > 30 * 32000:
            pcm = None
        lag = None
        if self.sample_source:
            lag = max(0, (self.sample_source.offset + len(self.sample_source.data)) / 32 - transcript.end_ms)
        self.input_records[iid] = {'transcript': transcript, 'pcm': pcm, 'at': monotonic(), 'asr_lag_ms': lag, 'received_at_ms': time() * 1000}
        self.sample_bytes += len(pcm or b'')
        while len(self.input_records) > 128 or self.sample_bytes > 16 * 1024 * 1024:
            old = next(iter(self.input_records))
            self.sample_bytes -= len(self.input_records.pop(old)['pcm'] or b'')
            self.annotations.pop(old, None)
            self.source_speakers.pop(old, None)
            self.candidate_cache.pop(old, None)
        self.input_idle.clear()
        await self.queue.put(transcript)

    async def request_emergency_interrupt(self, reason: str, *, expected_epoch=None) -> bool:
        """预留给明确紧急事件的入口；调用方负责判定，不做危险检测。"""
        if self.closed or (expected_epoch is not None and expected_epoch != self.epoch):
            return False
        self.record("voice_emergency_interrupt", {"reason": str(reason), "epoch": self.epoch})
        await self.stop("explicit stop")
        return True

    @staticmethod
    def unfinished(text):
        # A small conservative cue, not a claim to understand every utterance.
        text = text.strip().rstrip('，。！？,.!?… ')
        return text.endswith(('还有一种问题是', '问题是', '就是说', '比如说', '比方说', '然后', '因为', '如果', '但是', '的话', '不需要', '我想', '我觉得'))

    def assign_code(self, object_id: str, label: str) -> str:
        if not object_id:
            return ""
        code = next((item for item, oid in self.code_owner.items() if oid == object_id), "")
        if code:
            self.code_labels[object_id] = label or self.code_labels.get(object_id, "")
            return code
        code = f"P{len(self.code_owner) + 1}"
        self.code_owner[code] = object_id
        self.code_labels[object_id] = label or code
        return code

    def display_speaker(self, speaker, hint: str = "") -> str:
        if speaker.method == "unassigned_audio" or speaker.label == "声音归属待定":
            return "待定声音" + (f"（{hint}）" if hint else "")
        code = self.assign_code(speaker.object_id, speaker.label)
        if speaker.method in {"context_attribution", "main_context_attribution"} or (speaker.method == "jev_attribution" and speaker.status != "recognized"):
            tag = "，上下文推测"
        elif speaker.method == "self_introduction" or speaker.status == "introduced":
            tag = "，自我介绍"
        else:
            tag = ""
        return f"{code}（{speaker.label}{tag}）"

    def render_input(self, utterances, items=(), facts=()) -> str:
        kept = []
        marks = {item.n: item for item in items}
        for index, utterance in enumerate(utterances, start=1):
            item = marks.get(index)
            if item is not None and not item.keep:
                continue
            hint = ""
            if item is not None and item.speaker_level == "可能" and item.speaker_pick.startswith("P"):
                hint = f"可能是 {item.speaker_pick}"
            elif item is not None and item.speaker_pick == "new":
                hint = "可能是新来者"
            kept.append(f"{index}. {self.display_speaker(utterance.speaker, hint)}：{utterance.text}")
        lines = list(kept)
        if any(item.identity_only for item in items):
            lines.append('JEV 初判：仅终审人物；对话指向及是否回应由主认知判断。')
        if items:
            yes = [str(item.n) for item in items if item.to_jiangshi == "yes"]
            maybe = [str(item.n) for item in items if item.to_jiangshi == "maybe"]
            no = [str(item.n) for item in items if item.to_jiangshi == "no" and item.keep]
            if items and all(item.reason == "未能初判" for item in items):
                lines.append("JEV 初判：未能初判。")
            else:
                bits = []
                if yes:
                    bits.append("第 " + "、".join(yes) + " 条在对匠石说")
                if maybe:
                    bits.append("第 " + "、".join(maybe) + " 条可能在对匠石说")
                if no:
                    bits.append("第 " + "、".join(no) + " 条保留作背景，不直接对匠石说")
                if bits:
                    lines.append("JEV 初判：" + "；".join(bits) + "。")
        lines.extend(fact for fact in facts if fact)
        return "\n".join(lines)

    def restore_codes(self, text: str) -> str:
        import re
        for code, object_id in sorted(self.code_owner.items(), key=lambda item: len(item[0]), reverse=True):
            label = self.code_labels.get(object_id) or code
            text = re.sub(rf"{re.escape(code)}（[^）]*）",
                          lambda match: label + ("（上下文推测）" if "上下文推测" in match.group(0) else ""), text)
            text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(code)}(?!\d)", label, text)
        return text

    def object_of(self, token: str) -> str:
        return self.code_owner.get(token, self.sound_owner.get(token, token))

    def sound_code(self, utterance):
        source = self.source_speakers.get(utterance.input_id, utterance.speaker)
        if source.method != "anonymous_voice_cluster" or not source.object_id:
            return ""
        code = next((key for key, oid in self.sound_source.items() if oid == source.object_id), "")
        if not code:
            code = f"S{len(self.sound_owner) + 1}"
            self.sound_owner[code] = source.object_id
            self.sound_source[code] = source.object_id
        return code

    def recent_main_reviews(self, utterances=()):
        cutoff = min((u.received_at_ms for u in utterances if u.received_at_ms is not None), default=float("inf"))
        current_ids = {u.input_id for u in utterances}
        return [row["public"] for row in self.main_reviews
                if time() - row["at"] < 120 and not current_ids.intersection(row["input_ids"])
                and row["basis_at_ms"] <= cutoff and row.get('completed_at_ms', row['basis_at_ms']) <= cutoff]

    def _preserve_background(self, utterances):
        for u in utterances:
            self.process.note_entry_background(self.subject_id, u)

    def _capture_jev_inputs(self, current, items=(), *, entry_status=None):
        marks = {r.n: r for r in items}
        evidence_rows = self._jev_batch(current)
        for n, u in enumerate(current, 1):
            code = self.assign_code(u.speaker.object_id, u.speaker.label) if u.speaker.method not in {"unassigned_audio", "visual_scene"} else "unknown"
            row = {"p": code, "text": u.text, "at": u.received_at_ms, "basis": u.speaker.method,
                   "status": u.speaker.status, "track": u.speaker.track_id, "input_id": u.input_id,
                   "entry_status": entry_status or ('completed' if n in marks and marks[n].speaker_pick else 'failed'),
                   "voice": evidence_rows[n-1]['voice_evidence'],
                   "candidates": [{"p": c['who'], "similarity": c.get('score'), "source": c.get('source'),
                                   "names": c.get('names', [])} for c in evidence_rows[n-1]['candidates']],
                   "short_voice_hint": evidence_rows[n-1]['short_voice_hint']}
            initial = self.source_speakers.get(u.input_id, u.speaker)
            row["voice_initial"] = {"p": self.assign_code(initial.object_id, initial.label) if initial.method not in {"unassigned_audio", "visual_scene"} else "unknown",
                "basis": initial.method, "similarity": initial.confidence}
            if n in marks:
                item = marks[n]
                row["judgment"] = {"person": item.speaker_pick, "certainty": item.speaker_level,
                                   "why": item.speaker_reason or item.reason}
                if not item.identity_only:  # Historical adapters only.
                    row['judgment']['to'] = item.to_jiangshi
            self.jev_scene_store.append(u.input_id, row, codes=self.code_owner, labels=self.code_labels)

    def schedule_person_review(self, current, items, *, requested=False):
        # Events were captured with original batch indices before entry filtering.
        # This is only a coalesced notification, never a re-numbering of judgments.
        self._last_main_scene_trigger = monotonic()
        if self.person_reviewer is not None and not self.closed and self.person_review_queue.empty():
            self.person_review_queue.put_nowait('main')  # coalesced notification; events live on disk

    async def _person_reviews(self):
        loop = asyncio.get_running_loop()
        failures = 0
        while True:
            notified = False
            try:
                await asyncio.wait_for(self.person_review_queue.get(), timeout=1.0)
                notified, trigger = True, 'main'
            except asyncio.TimeoutError:
                if self.closed:
                    return
                if self.person_reviewer is None or monotonic() - self._last_scene_attempt < 10:
                    continue
                from jshi.voice.jev_scene import scene_refresh_reason
                pending = self.jev_scene_store.snapshot()
                oldest = min((r.get('queued_at') or r['payload'].get('at') or time()*1000 for r in pending['events']), default=time()*1000)
                trigger = scene_refresh_reason(pending, idle_seconds=monotonic()-self._last_main_scene_trigger,
                    pending_seconds=max(0, (time()*1000-oldest)/1000), idle_threshold=self.scene_idle_s,
                    input_count=self.scene_batch_inputs, input_chars=self.scene_batch_chars, max_pending_seconds=self.scene_max_wait_s)
                if not trigger:
                    continue
            try:
                while not self.closed:
                    snapshot = self.jev_scene_store.snapshot()
                    if not snapshot["events"] or self.person_reviewer is None:
                        break
                    # Writer selects a recent bounded window. The entire snapshot
                    # is retired atomically; omitted backlog is explicitly archived.
                    review_id = "jev-scene-" + uuid4().hex
                    self.jev_scene_calls.append(review_id)
                    def run():
                        from datetime import datetime
                        with self.process._write_lock:
                            main_scene = self.process.zone_store.render(self.subject_id, now=datetime.now().astimezone())
                        return self.person_reviewer.run(self.subject_id, snapshot, main_scene,
                            on_request=lambda req: self.process._record_step_input(req, self.person_reviewer.model,
                                subject_id=self.subject_id, activity_id=review_id))
                    started = monotonic()
                    self._last_scene_attempt = started
                    scene, raw = await loop.run_in_executor(self.person_review_executor, run)
                    accepted = not self.closed and self.jev_scene_store.commit(snapshot, scene)
                    self.record("voice_jev_scene", {"review_id": review_id, "accepted": accepted,
                        "trigger": trigger,
                        "model_raw": raw, "scene": scene, "chars": len(scene),
                        "version": self.jev_scene_store.snapshot()["version"],
                        "covered_input_ids": snapshot.get("material_window", {}).get("model_input_ids", []) if accepted else [],
                        "material_window": snapshot.get("material_window", {}),
                        "elapsed_ms": round((monotonic()-started)*1000)})
                    failures = 0
                    break  # further input is coalesced by the next main/fallback trigger
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.record("jev_scene_failed", {"error": f"{type(exc).__name__}: {exc}",
                    "model_raw": snapshot.get("model_raw", ""),
                    "pending_count": len(self.jev_scene_store.snapshot()["events"]), "attempt": failures})
                # No progress is committed. Retry on the next main turn, not a tight loop.
                await asyncio.sleep(min(2 ** min(failures, 4), 16))
            finally:
                if notified:
                    self.person_review_queue.task_done()

    def review_material(self, current, items, *, needs_main_review=False):
        # Evidence belongs to the independent review, never to main cognition.
        rows, public = [], []
        marks = {item.n: item for item in items}
        for index, u in enumerate(current, 1):
            item = marks.get(index)
            candidates, profiles = [], []
            for candidate in self.candidate_cache.get(u.input_id, ()):
                oid = candidate["object_id"]
                profile = self.process.profiles.get(oid)
                if profile is None or profile.status == "rejected":
                    continue
                code = self.assign_code(oid, profile.label)
                candidates.append({"who": code, "names": [profile.label, *profile.aliases],
                                   "score": candidate.get("score"), "source": candidate.get("source", "voiceprint"),
                                   **{key:candidate[key] for key in ('reference_count', 'stddev', 'sample_limited') if key in candidate}})
                profiles.append({"who": code, "candidate_only": True, "portrait_names": self.process.profiles.address_line(oid)})
            sound = self.sound_code(u)
            n = f"N{index}"
            voice = self.voice_evidence(u, candidates)
            rows.append({"n": n, "input_id": u.input_id, "allowed": [row["object_id"] for row in self.candidate_cache.get(u.input_id, ())], "sound": sound, "voice_evidence": voice})
            public.append({"n": n, "input_id": u.input_id, "text": u.text,
                "who": self.display_speaker(u.speaker), "sound": sound,
                "identity_confirmation": ("用户已手动将本句声音关联为 " + self.display_speaker(u.speaker) + "；旧临时声纹不再作为不同人物竞争，不因旧初判否认此次关联") if u.speaker.method == "manual_annotation" else "",
                "source": self.source_speakers.get(u.input_id, u.speaker).method,
                "seconds": round((u.end_ms - u.start_ms) / 1000, 2), "overlap": u.overlap,
                "short_voice_hint": {k:v for k,v in self.input_records.get(u.input_id, {}).get('short_voice_hint', {}).items() if k != 'candidates'},
                "candidates": [{k:v for k,v in candidate.items() if k != "score"} for candidate in candidates],
                "candidate_portraits": profiles, "voice_evidence": {k:v for k,v in voice.items() if k in {"pick", "strength", "basis"}},
                "jev": {k:v for k,v in asdict(item).items() if "score" not in k} if item else {"reason": "未能初判"}})
        text = json.dumps({"本批复判请求（非逐条开关）": bool(needs_main_review),
            "本批证据（N编号对应本批序号）": public}, ensure_ascii=False)
        return text, tuple(rows)

    def accept_person_review(self, result, snapshot):
        """Publish bounded advice for future JEV; never rewrite attribution here."""
        if self.closed or time() - snapshot['queued_at'] > 120:
            return
        from jshi.core.conversation_review import parse_review, parse_evidence_support
        judgments = parse_review(result)
        sources = {row['n']: row for row in snapshot['rows']}
        utterances = {u.input_id: u for u in snapshot['current']}
        accepted = []
        codes = snapshot['candidate_codes']
        owners = {code: oid for oid, code in codes.items()}
        for item in judgments['speaker_judgments']:
            row = sources.get(item['n'])
            if row is None or row['input_id'] not in utterances:
                continue
            if item.get('input_id') and item['input_id'] != row['input_id']:
                continue
            u = utterances[row['input_id']]
            oid = owners.get(item['speaker_pick'])
            if item['speaker_pick'] not in {'unknown', 'new'} and oid not in row['allowed']:
                continue
            # A manual association made after the snapshot supersedes old advice.
            latest = self.annotations.get(u.input_id, u.speaker)
            if latest.method == 'manual_annotation' and latest != u.speaker:
                continue
            item = {**item, **parse_evidence_support(item, voice=row.get('voice_evidence') or {},
                allowed={code for code, owner in owners.items() if owner in row['allowed']})}
            if item['evidence_relation'] == 'conflict' and item['level'] == '确定':
                item['level'] = '可能'
            elif item['evidence_relation'] == 'insufficient':
                item['level'], item['speaker_pick'] = '不确定', 'unknown'
                oid = None
            protected = latest.status in {'recognized', 'introduced'} or latest.method in {
                'voiceprint_match', 'self_introduction', 'manual_annotation'}
            if protected and oid and oid != latest.object_id:
                continue
            item = {**item, 'input_id': u.input_id}
            accepted.append(item)
            self.record('voice_identity_suggestion', {'input_id': u.input_id, **item,
                'source': '独立人物判断建议，交给下一次JEV评估，不改本轮与档案'})
        # An unvalidated free-text conclusion cannot bypass rejected judgments.
        # Free text may assign a valid P code to the wrong utterance even when
        # every structured judgment is valid. Only forward structured advice.
        note = ''
        if not accepted:
            return
        public = {'judgments': accepted, 'next_jev_note': note,
            'source': '独立人物判断建议，非人物原话、非声纹确认；JEV结合新证据决定',
            'basis': [{'n': row['n'], 'input_id': row['input_id'],
                'actor_id': utterances[row['input_id']].speaker.object_id,
                'sound': row['sound'], 'text': utterances[row['input_id']].text}
                for row in sources.values() if row['n'] in {item['n'] for item in accepted}]}
        self.main_reviews.append({'public': public, 'at': time(), 'input_ids': list(utterances),
            'basis_at_ms': snapshot['basis_at_ms'], 'completed_at_ms': int(time()*1000)})
        self.record('voice_person_review', {'review_id': snapshot['review_id'], **public,
            'model_next_jev_note': judgments['next_jev_note']})

    def apply_main_review(self, response, envelope, epoch):
        """Compatibility callback: resolve reply targets only, no identity judgment."""
        response = replace(response, speaker_judgments=(), next_jev_note='', object_assessment=None)
        if self.closed or epoch != self.epoch:
            return response, envelope, None
        current = envelope.current_utterances
        self.interaction_feedback.main([u.input_id for u in current], '')
        allowed_targets = {self.assign_code(u.speaker.object_id, u.speaker.label): u.speaker.object_id
            for u in current if u.speaker.method != 'unassigned_audio'}
        for u in current:
            sound = self.sound_code(u)
            if sound:
                allowed_targets[sound] = u.speaker.object_id
                self.sound_owner[sound] = u.speaker.object_id
        plan_items = []
        valid_ids = set(allowed_targets.values())
        for item in response.response_plan.items:
            resolved = tuple(dict.fromkeys(allowed_targets.get(token, token) for token in item.target_ids
                if allowed_targets.get(token, token) in valid_ids))
            plan_items.append(replace(item, target_ids=resolved))
        response = replace(response, response_plan=replace(response.response_plan, items=tuple(plan_items)))
        return response, envelope, self.identities.candidate(envelope.speaker)

    async def rank_candidates(self, transcript, speaker):
        record = self.input_records.get(transcript.input_id, {})
        if speaker.status in {"introduced", "recognized"}:
            try:
                matched = json.loads(transcript.identity_note or '{}')
            except ValueError:
                matched = {}
            stats = next((row for row in matched.get('candidates', ()) if row.get('object_id') == speaker.object_id), {})
            return [{**{key:stats[key] for key in ('reference_count', 'stddev', 'sample_limited') if key in stats},
                     "object_id": speaker.object_id, "label": speaker.label, "score": transcript.confidence, "source": speaker.method}], "already_identified"
        try:
            matched = json.loads(transcript.identity_note or "{}")
        except (ValueError, TypeError):
            matched = {}
        if isinstance(matched, dict) and isinstance(matched.get("candidates"), list) and matched["candidates"]:
            import math
            reused = []
            for candidate in matched["candidates"][:3]:
                if not isinstance(candidate, dict):
                    continue
                profile = self.process.profiles.get(candidate.get("object_id", ""))
                try:
                    score = float(candidate.get("score"))
                except (ValueError, TypeError):
                    continue
                if profile and profile.status != "rejected" and math.isfinite(score):
                    reused.append({"object_id": profile.object_id, "label": profile.label, "score": score, "source": "voiceprint"})
            return reused, "reused_voiceprint_candidates"
        pcm = record.get("pcm")
        if not self.local_speakers or not pcm:
            return [], "no_audio_or_model"
        short = len(pcm) < 48000
        if short and (transcript.overlap or len(pcm) < 6400):
            return [], "short_audio"
        if self.candidate_future is not None and not self.candidate_future.done():
            return [], "previous_candidate_still_running"
        def rank():
            if short:
                return self.local_speakers.rank_short_hint(pcm)
            import numpy as np
            samples = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768
            return self.local_speakers.rank_known(samples)
        self.candidate_future = asyncio.get_running_loop().run_in_executor(self.candidate_executor, rank)
        self.candidate_future.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        try:
            result = await asyncio.wait_for(asyncio.shield(self.candidate_future), self.candidate_timeout_s)
            if short:
                record['short_voice_hint'] = result
                return [{**row, 'source': 'repeated_short_audio'} for row in result.get('candidates', [])], "short_repeat_hint"
            return result, "ranked"
        except asyncio.TimeoutError:
            return [], "candidate_timeout"
        except Exception:
            return [], "candidate_error"

    def note_address(self, speaker, text: str, input_id: str, *, addressed=False) -> None:
        import re
        profiles = self.process.profiles
        if speaker.method in {"unassigned_audio", "context_attribution", "main_context_attribution", "voice_scene"}:
            return
        if speaker.status not in {"recognized", "introduced"} and speaker.method not in {"voiceprint_match", "anonymous_voice_cluster"}:
            return
        if not hasattr(profiles, "note_term"):
            return
        found = set(re.findall(r"匠石|匠大哥|将大哥|将士|匠哥", text))
        if addressed:
            for match in re.finditer(r"(?:我(?:以后)?(?:就)?叫你|我(?:可以|能不能)叫你|以后(?:就)?叫你)\s*([^，。！？?!\s]{1,12}?)(?=可不可以|好不好|可以吗|行不行|吗|[，。！？?!\s]|$)", text):
                found.add(match.group(1))
        leading = re.match(r"^(?:(?:hello|hi|你好|您好)[，,！!。\s]*)*(匠|将|姜|jiang)(?=[，,。！？!?\s]|$)", text.strip(), re.I) if addressed else None
        if leading:
            found.add(leading.group(1))
        for term in found:
            if term in text:
                profiles.note_term(speaker.object_id, "calls_subject", term, input_id)

    def note_person_names(self, speaker, text, input_id):
        import re
        if speaker.status not in {"introduced", "recognized"}:
            return
        profiles = self.process.profiles
        for match in re.finditer(r"(?:我也叫|也可以叫我|我的(?:昵称|别名)是|大家(?:都)?叫我)\s*([^，。！？?!\s]{1,30})(?=[，。！？?!\s]|$)", text):
            name = match.group(1)
            profiles.add_alias(speaker.object_id, name)
            if text[match.start():].startswith("大家"):
                profiles.note_term(speaker.object_id, "called_by_others", name, input_id)

    async def _writes(self) -> None:
        while True:
            job = await self.write_queue.get()
            try:
                wrote = await asyncio.get_running_loop().run_in_executor(self.write_executor, job)
                if wrote is False:
                    self.failed_write_jobs.append(job)
                    await self.emit("notice", text="写场未完成，输入与回应已保留；主流程继续，下一批合并重写。")
                    await self.emit("write_failed")
                elif wrote is True:
                    self.failed_write_jobs.clear()
                    await self.emit("write_completed")
            except Exception:
                self.failed_write_jobs.append(job)
                logging.getLogger(__name__).exception("写场队列失败")
                await self.emit("write_failed")
            finally:
                self.write_queue.task_done()

    async def retry_failed_writes(self) -> None:
        jobs, self.failed_write_jobs = self.failed_write_jobs, []
        if jobs:
            self.write_queue.put_nowait(jobs[-1])
        self.write_retried.set()

    @staticmethod
    def batch_text(utterances) -> str:
        return "\n".join(f"{u.speaker.label}：{u.text}" for u in utterances)

    def merge_envelopes(self, envelopes):
        envelope = envelopes[-1]
        pending = [u for previous in envelopes[:-1] for u in previous.current_utterances]
        if pending:
            current = tuple(sorted((*pending, *envelope.current_utterances), key=lambda u:
                (u.recorded_start_at_ms if u.recorded_start_at_ms is not None else
                 u.received_at_ms if u.received_at_ms is not None else u.start_ms, u.end_ms)))
            speakers = {u.speaker.object_id for u in current}
            evidence = envelope.speaker
            if len(speakers) > 1 or any(u.speaker.status == 'unknown' for u in current):
                if self.process.profiles.get(self.scene_id) is None:
                    self.process.profiles.create(ObjectProfile(self.scene_id, '语音现场', status='provisional', source='voice_scene'))
                evidence = SpeakerEvidence('scene', self.scene_id, '语音现场', 'unknown', 'voice_scene')
            start, end = min(u.start_ms for u in current), max(u.end_ms for u in current)
            envelope = replace(envelope, speaker=evidence, start_ms=start, end_ms=end,
                current_utterances=current, overlap=any(u.overlap for u in current),
                deferred_interrupt=any(e.deferred_interrupt for e in envelopes),
                jev_calls=tuple(call for item in envelopes for call in item.jev_calls),
                source=envelope.source if len({e.source for e in envelopes}) == 1 else 'mixed',
                parts=(InputPart('text', '\n'.join(e.text for e in envelopes)),
                       *tuple({(p.kind,p.reference):p for e in envelopes for p in e.parts if p.kind != 'text'}.values())))
        return envelope

    @staticmethod
    def envelope_items(envelope):
        """Reindex already judged batches by input ID after coalescing turns."""
        from jshi.voice.jev import BatchItem
        by_id = {}
        for call in envelope.jev_calls:
            ids = call.get('input_ids', ())
            for row in call.get('items', ()):
                n = row.get('n', 0)
                if type(n) is int and 1 <= n <= len(ids):
                    by_id[ids[n - 1]] = row
        return tuple(BatchItem(**{**by_id[u.input_id], 'n': n})
                     for n, u in enumerate(envelope.current_utterances, 1) if u.input_id in by_id)

    def enqueue_turn(self, envelope) -> None:
        pending = []
        while not self.turn_queue.empty():
            previous, _ = self.turn_queue.get_nowait()
            pending.append(previous)
        self.turn_queue.put_nowait((self.merge_envelopes([*pending, envelope]), self.epoch))
        for _ in pending:
            self.turn_queue.task_done()

    def voice_evidence(self, utterance, candidates):
        """Physical support uses original measurements, never a previous model guess."""
        import math
        original = self.source_speakers.get(utterance.input_id, utterance.speaker)
        if original.method == "voiceprint_match":
            return {"pick": self.assign_code(original.object_id, original.label), "strength": "clear", "basis": "声纹已匹配"}
        if original.method in {"anonymous_voice_cluster", "voice_continuity"}:
            return {"pick": self.assign_code(original.object_id, original.label),
                    "strength": "clear" if original.method == "anonymous_voice_cluster" and utterance.speaker.status != "introduced" else "weak",
                    "basis": "临时声音连续性；只认出同一个声音，未确认真实姓名"}
        physical = []
        for row in candidates:
            if row.get("source") != "voiceprint":
                continue
            try:
                score = float(row.get("score"))
            except (ValueError, TypeError):
                continue
            if math.isfinite(score):
                physical.append((score, row["who"]))
        physical.sort(reverse=True)
        if not physical:
            return {"pick": "", "strength": "unavailable", "basis": "没有本句有效的声纹比较；近期人物和自我介绍另作语义参考"}
        threshold = getattr(self.local_speakers, "confirmation_threshold", getattr(self.local_speakers, "threshold", None))
        margin = getattr(self.local_speakers, "margin", None)
        score, code = physical[0]
        gap = score - physical[1][0] if len(physical) > 1 else None
        clear = threshold is not None and margin is not None and score >= threshold and (gap is None or gap >= margin)
        return {"pick": code, "strength": "clear" if clear else "weak", "score": score,
                "gap": gap, "threshold": threshold, "margin": margin,
                "basis": "本句声纹比较；相似度不是概率"}

    def _jev_batch(self, utterances):
        batch = []
        for index, utterance in enumerate(utterances, start=1):
            if utterance.speaker.method == 'visual_scene':
                batch.append({'n':index,'input_id':utterance.input_id,'track':'','sound':'',
                              'who':'unknown','text':utterance.text,'seconds':0,'basis':'visual_scene',
                              'at':utterance.received_at_ms,'at_ms':utterance.start_ms,'candidates':[],
                              'short_voice_hint':{},'source':'visual_environment',
                              'voice_evidence':{'strength':'unavailable','basis':'视觉观察，无声音身份'}})
                continue
            candidates = []
            for item in self.candidate_cache.get(utterance.input_id, []):
                code = self.assign_code(item["object_id"], item.get("label") or "")
                profile = self.process.profiles.get(item["object_id"])
                candidates.append({"who": code, "score": item.get("score"), "source": item.get("source") or "voiceprint",
                    **{key:item[key] for key in ('reference_count', 'stddev', 'sample_limited') if key in item},
                    "names": [profile.label, *profile.aliases] if profile else [],
                    "portrait_names": self.process.profiles.address_line(item["object_id"])})
            batch.append({"n": index, "input_id": utterance.input_id, "track": utterance.speaker.track_id,
                          "sound": self.sound_code(utterance),
                          "who": self.display_speaker(utterance.speaker), "text": utterance.text,
                          "seconds": round(max(0, utterance.end_ms - utterance.start_ms) / 1000, 2),
                          "basis": utterance.speaker.method, "at": utterance.received_at_ms, "at_ms": utterance.start_ms, "candidates": candidates,
                          "short_voice_hint": self.input_records.get(utterance.input_id, {}).get('short_voice_hint', {}),
                          "voice_evidence": self.voice_evidence(utterance, candidates)})
        return batch

    def _jev_context(self, utterances):
        current_ids = {u.input_id for u in utterances}
        cutoff = min((u.received_at_ms for u in utterances if u.received_at_ms is not None), default=time()*1000)
        _, context = self.jev_scene_store.view(cutoff, current_ids)
        heard = [r for r in self.played_history if r["at"] <= cutoff]
        return context, "\n".join(r["text"] for r in heard) or self.prior_session_spoken

    def _apply_attribution(self, utterances, items):
        updated = []
        for index, utterance in enumerate(utterances, start=1):
            item = next((it for it in items if it.n == index), None)
            speaker = utterance.speaker
            if speaker.method == 'visual_scene':
                updated.append(utterance)
                self.annotations[utterance.input_id] = speaker
                continue
            if self.local_speakers is not None and hasattr(self.local_speakers, 'review_candidate'):
                code = self.assign_code(speaker.object_id, speaker.label)
                agreement = bool(item and speaker.status == 'recognized' and speaker.method == 'voiceprint_match'
                                 and item.evidence_relation == 'agree' and item.voice_pick == code
                                 and item.semantic_pick == code and item.semantic_reason)
                if agreement:
                    self._sample_job(utterance.input_id, 'voice_candidate_review',
                        lambda iid=utterance.input_id, oid=speaker.object_id:
                        self.local_speakers.review_candidate(iid, oid, agreement=True))
            if item and item.speaker_pick and speaker.method != "manual_annotation":
                initial = self.source_speakers.get(utterance.input_id, speaker)
                oid = self.code_owner.get(item.speaker_pick)
                allowed = {r["object_id"] for r in self.candidate_cache.get(utterance.input_id, ())
                    if r.get("source") != "repeated_short_audio" or
                    (item.semantic_pick == item.speaker_pick and item.semantic_reason)}
                allowed.add(speaker.object_id)
                if oid in allowed and item.speaker_level in {"确定", "可能"}:
                    profile = self.process.profiles.get(oid)
                    if profile is not None:
                        speaker = replace(speaker, object_id=oid, label=profile.label,
                            method="jev_attribution", status="recognized" if item.speaker_level == "确定" else "provisional")
                elif item.speaker_pick in {"unknown", "new"} and item.speaker_reason and initial.method == "voiceprint_match":
                    speaker = replace(speaker, object_id="input:" + utterance.input_id, label="声音归属待定",
                        method="unassigned_audio", status="unknown", confidence=None, voiceprint_id="")
                self.record("voice_attribution", {"input_id": utterance.input_id,
                    "voice_initial": asdict(initial), "final": asdict(speaker),
                    "pick": item.speaker_pick, "level": item.speaker_level,
                    "reason": item.speaker_reason, "voice_pick": item.voice_pick,
                    "evidence_relation": item.evidence_relation})
            updated.append(replace(utterance, speaker=speaker))
            self.annotations[utterance.input_id] = speaker
            if item and item.to_jiangshi == "yes":
                self.note_address(speaker, utterance.text, utterance.input_id, addressed=True)
                self.interaction_feedback.incoming(utterance.input_id, speaker.object_id, utterance.text,
                    directed=True, received_at_ms=utterance.received_at_ms)
        by_id = {u.input_id: u for u in updated if u.input_id}
        self.scene = deque((by_id.get(u.input_id, u) for u in self.scene), maxlen=24)
        return updated

    async def _publish_attribution(self, utterances, items, record):
        """Update existing input rows after final entry attribution, including failures."""
        marks = {item.n: item for item in items}
        for n, u in enumerate(utterances, 1):
            item = marks.get(n)
            completed = bool(record.get("judged") and item and item.speaker_pick)
            await self.emit("identity_final", input_id=u.input_id, speaker=asdict(u.speaker),
                voice_initial=asdict(self.source_speakers.get(u.input_id, u.speaker)),
                jev_status="completed" if completed else "failed",
                certainty=item.speaker_level if completed else "",
                reason=(item.speaker_reason or item.reason) if completed else record.get("model_error") or record.get("reason", "未能初判"))

    def _sample_job(self, input_id, event_type, work):
        """A bounded serial worker keeps sampling and review outside entry latency."""
        if self.closed or len(self.sample_jobs) >= 32:
            return 'queue_limit'
        future = asyncio.get_running_loop().run_in_executor(self.sample_executor, work)
        self.sample_jobs.add(future)
        def completed(done):
            self.sample_jobs.discard(done)
            try:
                status = done.result()
                if not self.closed:
                    row = self.input_records.get(input_id)
                    if row is not None:
                        row['voice_candidate_status'] = status
                    self.record(event_type, {'input_id': input_id, 'status': status})
            except BaseException as exc:
                if not self.closed:
                    self.record('voice_candidate_error', {'input_id': input_id, 'error': str(exc)[:160]})
        future.add_done_callback(completed)
        return 'queued'

    async def _judge(self, utterances, speaker, overlap, envelope=None) -> tuple[InterruptDecision, dict, tuple]:
        from jshi.voice.jev import BatchItem
        snapshot = self.delivery.snapshot()
        if self.active_turn and (not snapshot or snapshot.get("state") in {"completed", "stopped", "failed"}):
            snapshot = self.active_turn
        context, spoken = self._jev_context(utterances)
        names = ["匠石"]
        if hasattr(self.process.profiles, "all_terms"):
            names.extend(term for term in self.process.profiles.all_terms("calls_subject") if term not in names)
        # 还没有交付记录时，状态是空的。空状态不能当成正在播放，
        # 否则没有模型的初判会一律选择继续播放，话进不了主流程。
        state = (snapshot or {}).get("state") or "completed"
        delivery = {"state": state, "spoken": spoken[:200],
                    "pending": (snapshot or {}).get("pending_text", "")[:200], "names_for_jiangshi": names,
                    "attention": self.attention.hint(speaker, overlap=overlap),
                    "jev_scene": self.jev_scene_store.view(
                        min((u.received_at_ms for u in utterances if u.received_at_ms is not None), default=time()*1000))[0],
                    "now": time()*1000}
        if envelope is None:
            source = 'web_text' if utterances and all(u.speaker.method == 'user_selected' for u in utterances) else 'voice'
            parts = (InputPart('text', self.batch_text(utterances)),)
            if source == 'voice' and utterances:
                parts += (InputPart('audio', reference=f'session:{self.session_id}:{utterances[0].start_ms}-{utterances[-1].end_ms}', media_type='audio/pcm'),)
            envelope = InputEnvelope(self.session_id, source, parts, speaker, current_utterances=tuple(utterances))
        envelope = pack_input(envelope, getattr(self.process, 'vision', None), self.subject_id)
        delivery['envelope'] = packet(envelope, getattr(self.process, 'vision', None), self.subject_id)
        cutoff = min((u.received_at_ms for u in utterances if u.received_at_ms is not None), default=float("inf"))
        feedback = self.interaction_feedback.snapshot(cutoff_ms=cutoff)
        for row in feedback['questions']:
            row['targets'] = [self.assign_code(oid, self.code_labels.get(oid, '')) for oid in row.pop('target_ids')]
        delivery['interaction_feedback'] = feedback
        metrics = self.voice_timings.get(self.reply_epochs.get((snapshot or {}).get("reply_id")), (None, {}))[1]
        if metrics.get("reply_ready_at_ms", 0) > cutoff:
            delivery["pending"] = ""
        batch = self._jev_batch(utterances)
        for row, u in zip(batch, utterances):
            row['source'] = ('visual_environment' if u.speaker.method == 'visual_scene' else
                             'web_text' if u.speaker.method == 'user_selected' else 'voice')
            if row['source'] == 'visual_environment':
                row.update(who='unknown', candidates=[], voice_evidence={'strength':'unavailable','basis':'视觉观察，无声音身份'})
        if not batch:
            batch = [{'n': 1, 'text': envelope.text, 'who': 'unknown', 'at': time()*1000, 'source': envelope.source, 'voice_evidence': {'strength':'unavailable','basis':'非声音观察'}}]
        started = monotonic()
        for u in utterances:
            if u.input_id in self.input_records:
                self.input_records[u.input_id]['jev_started_at'] = started
        path, timed_out = "model", False
        prompt_activity_id = "jev-" + uuid4().hex
        sent_requests = []

        def capture_request(request):
            sent_requests.append(request)
            self.process._record_step_input(request, self.jev.model, subject_id=self.subject_id,
                                            activity_id=prompt_activity_id)

        def open_batch(reason):
            items = tuple(BatchItem(int(item["n"]), "maybe", 0.5, reason) for item in batch)
            return BatchDecision("respond", reason, items, "", False, model_error=reason)

        if self.jev_future is not None and not self.jev_future.done():
            path = "rule"
            result = open_batch("上一次初判尚未结束")
        else:
            loop = asyncio.get_running_loop()

            def run():
                if hasattr(self.jev, "decide_batch"):
                    if isinstance(self.jev, VoiceJEV) and type(self.jev).decide_batch is VoiceJEV.decide_batch:
                        return self.jev.decide_batch(self.subject_id, batch, context, delivery,
                                                     overlap=overlap, on_request=capture_request)
                    return self.jev.decide_batch(self.subject_id, batch, context, delivery, overlap=overlap)
                decision = self.jev.decide(self.subject_id, "\n".join(item["text"] for item in batch),
                                           asdict(speaker), delivery, overlap=overlap)
                return VoiceJEV()._from_legacy(decision, batch)

            self.jev_future = loop.run_in_executor(self.jev_executor, run)
            try:
                result = await asyncio.wait_for(asyncio.shield(self.jev_future), self.timeout_s)
            except asyncio.TimeoutError:
                path, timed_out = "timeout_rule", True
                result = open_batch("未能初判")
        decision = InterruptDecision(result.action, result.reason, result.claimed_name)
        record = {"ms": round((monotonic() - started) * 1000), "action": decision.action,
                  "path": path, "timed_out": timed_out, "reason": decision.reason[:80],
                  "items": [asdict(item) for item in result.items], "needs_main_review": result.needs_main_review,
                  "input_ids": [u.input_id for u in utterances],
                  "main_prompt_hint": result.main_prompt_hint, "prompt_policy_version": 3,
                  "identity_only": bool(result.items) and all(item.identity_only for item in result.items),
                  "prompt_activity_id": prompt_activity_id,
                  "prompt_requested": bool(sent_requests) if type(self.jev) is VoiceJEV else None,
                  "judged": result.judged, "model_error": result.model_error}
        record['routing_error'] = result.routing_error
        record['visual_snapshot_id'] = envelope.visual_snapshot_id
        record['input_parts'] = [asdict(p) for p in envelope.parts]
        self.record('voice_jev_diagnostics', {**record, 'model_raw': result.model_raw,
            'model_error': result.model_error})
        for u in utterances:
            if u.input_id in self.input_records:
                self.input_records[u.input_id]['jev_finished_at'] = monotonic()
        if (timed_out or result.model_error) and monotonic() - self._last_jev_notice >= 30:
            self._last_jev_notice = monotonic()
            message = 'JEV接口返回402，可能是余额或计费限制；本轮入口判断失败，声音初判和原话仍保留。' if '402' in result.model_error else 'JEV未能完成入口判断，已保留原话交主流程处理；这不是人物“不确定”的正常判断。'
            await self.emit('notice', text=message)
        elif result.routing_error and monotonic() - self._last_jev_notice >= 30:
            self._last_jev_notice = monotonic()
            await self.emit('notice', text=result.routing_error)
        return decision, record, result.items

    async def _work(self) -> None:
        while True:
            transcript = await self.queue.get()
            worker_started = monotonic()
            batch = [transcript]
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.input_max_batch_s
                while len(batch) < 64:
                    # Explicit stop is not delayed by conversational batching.
                    if not batch[-1].overlap and VoiceJEV().decide(self.subject_id, batch[-1].text, {}, {}).action == 'stop':
                        break
                    pause_s = max(self.input_pause_s, 4.0) if self.unfinished(batch[-1].text) else self.input_pause_s
                    remaining = min(deadline - loop.time(), pause_s - (loop.time() - self.last_input_at))
                    if remaining <= 0:
                        break
                    try:
                        batch.append(await asyncio.wait_for(self.queue.get(), remaining))
                    except asyncio.TimeoutError:
                        continue
                batch.sort(key=lambda t: (t.start_ms, t.end_ms))
                group_ready = monotonic()
                binding_notes = []
                current = []
                from jshi.voice.jev import explicit_name
                for transcript in batch:
                    input_record = self.input_records.get(transcript.input_id, {})
                    input_record["group_ready_at"] = group_ready
                    input_record["worker_started_at"] = max(worker_started, input_record.get("at", worker_started))
                    identity_started = monotonic()
                    evidence = self.identities.resolve(transcript.track_id, voiceprint_id=transcript.voiceprint_id, confidence=transcript.confidence, cluster_id=transcript.speaker_cluster_id, uncertain=transcript.identity_uncertain, tentative=transcript.identity_tentative)
                    self.source_speakers[transcript.input_id] = evidence
                    answer = self.identities.name_answer(evidence, transcript.text) if not transcript.overlap else ''
                    claimed = (explicit_name(transcript.text) or answer) if not transcript.overlap else ""
                    remembered = False
                    if self.local_speakers is not None and evidence.method == 'anonymous_voice_cluster':
                        remembered = await asyncio.to_thread(self.local_speakers.remember_anonymous,
                            transcript.speaker_cluster_id or transcript.track_id, evidence.object_id)
                    if claimed:
                        evidence, binding_note = self.identities.introduce(transcript.track_id, claimed,
                            basis='name_answer' if answer else 'explicit_self_introduction',
                            cluster_id=transcript.speaker_cluster_id, uncertain=transcript.identity_uncertain)
                        if evidence.status == "introduced" and self.local_speakers is not None:
                            learned = self.local_speakers.bind(transcript.track_id, evidence.object_id)
                            if not learned and transcript.speaker_cluster_id and not transcript.identity_uncertain:
                                learned = self.local_speakers.bind(transcript.speaker_cluster_id, evidence.object_id)
                            if not learned and answer:
                                cluster = next((c for c, e in self.identities.clusters.items() if e.object_id == evidence.object_id), '')
                                if cluster:
                                    learned = self.local_speakers.bind(cluster, evidence.object_id)
                            binding_note += (" 声纹特征已保存。" if learned else " 样本不足，声纹尚未保存。")
                        if evidence.status == 'introduced' and self.local_speakers is not None:
                            for old_id, linked in list(self.identities.associations.items()):
                                old = self.process.profiles.get(old_id)
                                if linked.object_id == evidence.object_id and old_id != evidence.object_id and old and old.label.startswith('未命名访客'):
                                    self.local_speakers.forget(old_id)
                            self.local_speakers.confirm(evidence.object_id)
                        binding_notes.append(binding_note)
                        if evidence.status == "introduced":
                            self.attention.engage((evidence.object_id,))
                        await self.emit("identity", **asdict(evidence), detail=binding_note)
                    if remembered and evidence.label.startswith('未命名访客') and evidence.object_id not in self.remembered_voices:
                        self.remembered_voices.add(evidence.object_id)
                        await self.emit('notice', text=f'{evidence.label} 是内部名，还不知道姓名。声音暂存；自报先记称呼线索，正式关联按人工确认。')
                    received = self.input_records.get(transcript.input_id, {}).get('received_at_ms')
                    origin = self.sample_source.started_at_ms if self.sample_source else None
                    utterance = SceneUtterance(transcript.text, evidence, transcript.start_ms, transcript.end_ms, transcript.overlap, transcript.input_id,
                        origin + transcript.start_ms if origin is not None else None,
                        origin + transcript.end_ms if origin is not None else None, received, transcript.identity_note)
                    self.note_address(evidence, transcript.text, transcript.input_id)
                    self.note_person_names(evidence, transcript.text, transcript.input_id)
                    input_record["identity_ms"] = round((monotonic() - identity_started) * 1000)
                    pcm = (self.input_records.get(transcript.input_id) or {}).get("pcm")
                    if self.local_speakers is not None and hasattr(self.local_speakers, 'collect_candidate'):
                        actor = evidence.object_id if evidence.method != 'unassigned_audio' else 'input:' + transcript.input_id
                        input_record['voice_candidate_status'] = self._sample_job(transcript.input_id, 'voice_candidate_collected',
                            lambda pcm=pcm, t=transcript, actor=actor: self.local_speakers.collect_candidate(pcm, t.input_id,
                                actor, self.session_id, overlap=t.overlap, start_ms=t.start_ms, end_ms=t.end_ms))
                    candidate_started = monotonic()
                    found, candidate_status = await self.rank_candidates(transcript, evidence)
                    input_record["candidate_ms"] = round((monotonic() - candidate_started) * 1000)
                    input_record["candidate_status"] = candidate_status
                    if input_record.get('short_voice_hint'):
                        try:
                            note = json.loads(utterance.identity_note or '{}')
                        except ValueError:
                            note = {'reason': utterance.identity_note}
                        utterance = replace(utterance, identity_note=json.dumps(
                            {**note, 'short_voice_hint': input_record['short_voice_hint']}, ensure_ascii=False))
                    if not found and transcript.end_ms - transcript.start_ms < 1500:
                        seen = []
                        for old in reversed(self.scene):
                            if old.speaker.method in {"unassigned_audio", "voice_scene"} or not old.speaker.object_id:
                                continue
                            if old.speaker.object_id not in {oid for oid, _ in seen}:
                                seen.append((old.speaker.object_id, old.speaker.label))
                            if len(seen) >= 3:
                                break
                        found = [{"object_id": oid, "label": label, "score": None, "source": "recent"} for oid, label in seen]
                    self.candidate_cache[transcript.input_id] = found
                    profile = self.process.profiles.get(evidence.object_id)
                    if profile and profile.source == 'voice_anonymous' and evidence.method in {'anonymous_voice_cluster', 'voice_continuity'}:
                        self.candidate_cache[transcript.input_id] = [{"object_id": evidence.object_id, "label": evidence.label,
                            "score": None, "source": "temporary_voice"}, *[row for row in found if row["object_id"] != evidence.object_id]]
                    self.scene.append(utterance)
                    if evidence.status == "unknown":
                        saved = self.unknown_inputs.append(utterance.input_id,
                            "input:" + utterance.input_id if evidence.method == "unassigned_audio" else evidence.object_id,
                            transcript.text, self.session_id, transcript.start_ms, transcript.end_ms)
                        if not saved:
                            await self.emit("notice", text="未知人物暂存已达到500MB上限；本轮交往仍继续保留，暂不增加独立历史。")
                    current.append(utterance)
                    self.last_track = transcript.track_id
                    self.record("voice_scene_input", asdict(utterance))
                    await self.emit("timeline", **asdict(utterance))
                    await self.emit("speaker", **asdict(evidence), jev_status="pending")
                evidence = current[-1].speaker
                active = self.delivery.snapshot().get("state") in {"playing", "paused", "queued"} or self.active_turn is not None
                explicit_stop = any(not u.overlap and VoiceJEV().decide(self.subject_id, u.text, {}, {}).action == 'stop' for u in current)
                busy = self.active_turn is not None and not explicit_stop
                jev_calls = ()
                items = ()
                if explicit_stop:
                    decision = InterruptDecision('stop', 'explicit stop')
                else:
                    decision, record, items = await self._judge(current, evidence, any(u.overlap for u in current))
                    current = self._apply_attribution(current, items)
                    await self._publish_attribution(current, items, record)
                    jev_calls = (record,)
                # Publish only after entry judgment/rule completion. Background
                # snapshots must never race ahead of this batch's first JEV.
                self._capture_jev_inputs(current, items, entry_status='rule' if explicit_stop else None)
                self.pending_voice = False
                if busy:
                    await self.emit('input_pending', count=len(current))
                else:
                    self.last_jev = asdict(decision)
                    self.record("voice_jev", {"input": self.render_input(current, items), "track_id": transcript.track_id, **asdict(decision)})
                    await self.emit("jev", **asdict(decision))
                if decision.action in {"resume", "ignore"}:
                    self._preserve_background(current)
                    self._capture_jev_inputs(current, items)
                    self.unreported_jev.extend(jev_calls)
                    await self.noise()
                    continue  # Scene/history already recorded; no main reply.
                elif decision.action == "stop" or (active and not busy and not (jev_calls and (jev_calls[-1].get("timed_out") or jev_calls[-1].get('identity_only') or not jev_calls[-1].get('judged')))):
                    await self.stop(decision.reason)
                if decision.action == "stop":
                    await self.emit("notice", text="已停止播放。")
                    continue
                facts = []
                if self.unfinished(current[-1].text):
                    facts.append(f"第 {len(current)} 条话还没说完。")
                for utterance in current:
                    if utterance.speaker.label.startswith('未命名访客') and utterance.speaker.method != 'unassigned_audio' and utterance.speaker.object_id not in self.identities.name_questions:
                        facts.append(f"{self.display_speaker(utterance.speaker)} 第一次说话，还不知道姓名。")
                        break
                text = self.render_input(current, items, facts)
                if len(text) > 8000:
                    raise ValueError("语音发言过长，请分段说")
                if evidence.status == "unknown" or len({u.speaker.track_id for u in current}) > 1:
                    if self.process.profiles.get(self.scene_id) is None:
                        self.process.profiles.create(ObjectProfile(self.scene_id, "语音现场", status="provisional", source="voice_scene"))
                    evidence = SpeakerEvidence("scene", self.scene_id, "语音现场", "unknown", "voice_scene")
                envelope = InputEnvelope(
                    session_id=self.session_id, source="microphone",
                    parts=(InputPart("text", text), InputPart("audio", reference=f"session:{self.session_id}:{current[0].start_ms}-{max(u.end_ms for u in current)}", media_type="audio/pcm")),
                    speaker=evidence, start_ms=min(u.start_ms for u in current), end_ms=max(u.end_ms for u in current),
                    overlap=any(u.overlap for u in current), delivery_context="",
                    utterances=tuple(self.scene), current_utterances=tuple(current), deferred_interrupt=False,
                    jev_calls=jev_calls,
                )
                ready_at = monotonic()
                for u in current:
                    if u.input_id in self.input_records:
                        self.input_records[u.input_id]["ready_at"] = ready_at
                self.enqueue_turn(envelope)
            except Exception as exc:
                await self.emit("notice", text=f"语音轮次失败：{type(exc).__name__}：{exc}")
            finally:
                for _ in batch:
                    self.queue.task_done()
                if self.queue.empty():
                    self.input_idle.set()

    async def _turns(self) -> None:
        while True:
            # Each queued envelope is already a completed speech group. New
            # speech must not hold ready groups hostage via a global idle flag.
            envelope, epoch = await self.turn_queue.get()
            # Writing is independent; cognition sees committed scene + pending events.
            write_wait_ms = 0
            pending = [envelope]
            while not self.turn_queue.empty():
                newer, epoch = self.turn_queue.get_nowait()
                pending.append(newer)
                self.turn_queue.task_done()
            envelope = self.merge_envelopes(pending)
            try:
                if epoch != self.epoch:
                    continue
                def annotated(u): return replace(u, speaker=self.identities.associated(self.annotations.get(u.input_id, u.speaker)))
                current = tuple(annotated(u) for u in envelope.current_utterances)
                from jshi.voice.jev import BatchItem
                items = ()
                if envelope.deferred_interrupt:
                    decision, record, items = await self._judge(current, envelope.speaker, envelope.overlap, envelope)
                    current = tuple(self._apply_attribution(list(current), items))
                    await self._publish_attribution(current, items, record)
                    envelope = replace(envelope, jev_calls=(*envelope.jev_calls, record),
                        visual_snapshot_id=record.get('visual_snapshot_id', ''),
                        parts=tuple(InputPart(**p) for p in record.get('input_parts', ())))
                    await self.emit('jev', **asdict(decision))
                    self.last_jev = asdict(decision)
                    self.record('voice_jev', {'input': self.render_input(current, items), **asdict(decision)})
                    if decision.action in {'ignore', 'resume', 'stop'}:
                        self.unreported_jev.extend(envelope.jev_calls)
                    if decision.action in {'ignore', 'resume'}:
                        self._preserve_background(current)
                        self._capture_jev_inputs(current, items)
                        await self.noise()
                        continue
                    playing = self.delivery.snapshot().get('state') in {'playing', 'paused', 'queued'}
                    if decision.action == 'stop' or (playing and record.get('judged') and not record.get('timed_out') and not record.get('identity_only')):
                        await self.stop(decision.reason)
                        epoch = self.epoch
                    if decision.action == 'stop':
                        continue
                elif envelope.jev_calls and envelope.jev_calls[-1].get('items'):
                    items = self.envelope_items(envelope)
                facts = []
                delivery = self.delivery.snapshot()
                previous = self.voice_timings.get(self.reply_epochs.get(delivery.get('reply_id')))
                if previous and previous[1].get('reply_ready_at_ms') and any(
                        u.received_at_ms is not None and u.received_at_ms <= previous[1]['reply_ready_at_ms'] for u in current):
                    facts.append('有的话在匠石上一轮回应生成之前就已说出。')
                if delivery.get('state') in {'playing', 'paused'} and (delivery.get('partial_text') or delivery.get('played_text')):
                    heard = delivery.get('partial_text') or delivery.get('played_text')
                    facts.append(f'匠石上一轮回应播到“{heard[:40]}”时还没说完。')
                self._capture_jev_inputs(current, items)
                text = self.render_input(current, items, facts)
                if any(p.kind == 'image' for p in envelope.parts) and envelope.source == 'visual_environment':
                    text = envelope.text
                marks = {item.n: item for item in items}
                annotations = tuple({'input_id': u.input_id, 'n': f'N{n}',
                    'direction': marks[n].to_jiangshi if n in marks else '',
                    'relevance': marks[n].relevance if n in marks else ''}
                    for n, u in enumerate(current, 1))
                cutoff = min((u.received_at_ms for u in current if u.received_at_ms is not None), default=time()*1000)
                interaction_scene, _ = self.jev_scene_store.view(cutoff, {u.input_id for u in current})
                envelope = replace(envelope, review_context='', review_candidates=(), input_annotations=annotations,
                                   interaction_scene=interaction_scene)
                for u in current:
                    if u.input_id in self.input_records:
                        self.input_records[u.input_id]["write_barrier_ms"] = write_wait_ms
                if items:
                    dropped = {item.n for item in items if not item.keep}
                    for index, utterance in enumerate(current, 1):
                        if index in dropped:
                            self._preserve_background((utterance,))
                    for index, utterance in enumerate(current, start=1):
                        if index in dropped and utterance.input_id:
                            await self.emit('input_route', input_id=utterance.input_id, kept=False)
                    current = tuple(utterance for index, utterance in enumerate(current, start=1) if index not in dropped)
                    if not current:
                        await self.noise()
                        continue
                if current:
                    speaker = current[-1].speaker if len({u.speaker.object_id for u in current}) == 1 else envelope.speaker
                    if speaker.method in {"context_attribution", "jev_attribution"} and speaker.status != "recognized":
                        if self.process.profiles.get(self.scene_id) is None:
                            self.process.profiles.create(ObjectProfile(self.scene_id, "语音现场", status="provisional", source="voice_scene"))
                        speaker = SpeakerEvidence("scene", self.scene_id, "语音现场", "unknown", "voice_scene")
                    envelope = replace(envelope, current_utterances=current,
                        utterances=current, speaker=speaker,
                        delivery_context='',
                        parts=(InputPart('text', text), *envelope.parts[1:]))
                if envelope.jev_calls and not envelope.visual_snapshot_id:
                    call = envelope.jev_calls[-1]
                    envelope = replace(envelope, visual_snapshot_id=call.get('visual_snapshot_id', ''))
                    images = tuple(InputPart(**p) for p in call.get('input_parts', ()) if p['kind'] == 'image')
                    envelope = replace(envelope, parts=tuple(p for p in envelope.parts if p.kind != 'image') + images)
                self.active_turn = {"state": "thinking", "object_id": envelope.speaker.object_id,
                                    "input": envelope.text, "pending_text": "尚未生成回应"}
                self.schedule_person_review(current, items)
                await self._respond(envelope, epoch)
            except Exception as exc:
                await self.emit("notice", text=f"主流程调用失败：{exc}")
            finally:
                self.active_turn = None
                await self.emit('turn_complete')
                self.turn_queue.task_done()

    async def _respond(self, envelope, epoch: int) -> None:
        loop = asyncio.get_running_loop()
        emitted = False
        self.turn_sequence += 1
        turn_key = (epoch, self.turn_sequence)
        self.reply_input_ids[turn_key] = [u.input_id for u in envelope.current_utterances]
        while len(self.reply_input_ids) > 32:
            self.reply_input_ids.pop(next(iter(self.reply_input_ids)))
        began = monotonic()
        rows = [self.input_records[u.input_id] for u in envelope.current_utterances if u.input_id in self.input_records]
        calls = [*self.unreported_jev, *envelope.jev_calls]
        self.unreported_jev.clear()
        metrics = {'queue_wait_ms': round((began - min(r['at'] for r in rows)) * 1000) if rows else None,
                   'asr_lag_ms': max((r['asr_lag_ms'] for r in rows if r.get('asr_lag_ms') is not None), default=None),
                   'jev_ms': sum(call['ms'] for call in calls), 'jev_calls': calls}
        current_ids = {u.input_id for u in envelope.current_utterances}
        metrics['current_jev_ms'] = sum(call['ms'] for call in calls if current_ids.intersection(call.get('input_ids', ())))
        metrics['historical_jev_ms'] = metrics['jev_ms'] - metrics['current_jev_ms']
        metrics['input_timings'] = [{
            'input_id': u.input_id, 'received_at_ms': r.get('received_at_ms'),
            'speech_end_at_ms': u.recorded_end_at_ms,
            'collection_wait_ms': round(max(0, r.get('group_ready_at', r['at']) - r['at']) * 1000),
            'identity_ms': r.get('identity_ms', 0), 'candidate_ms': r.get('candidate_ms', 0),
            'jev_ms': round(max(0, r.get('jev_finished_at', r['at']) - r.get('jev_started_at', r['at'])) * 1000),
            'ready_queue_wait_ms': round(max(0, began - r.get('ready_at', began)) * 1000),
        } for u in envelope.current_utterances if (r := self.input_records.get(u.input_id)) is not None]
        metrics['speech_end_at_ms'] = max((u.recorded_end_at_ms for u in envelope.current_utterances
                                          if u.recorded_end_at_ms is not None), default=None)
        metrics.update({"speech_group_wait_ms": max((round((r.get("group_ready_at", r["at"]) - r["at"]) * 1000) for r in rows), default=0),
            "input_worker_wait_ms": max((round((r.get("worker_started_at", r["at"]) - r["at"]) * 1000) for r in rows), default=0),
            "speech_collection_ms": max((round((r.get("group_ready_at", r["at"]) - r.get("worker_started_at", r["at"])) * 1000) for r in rows), default=0),
            "identity_ms": sum(r.get("identity_ms", 0) for r in rows),
            "candidate_ms": sum(r.get("candidate_ms", 0) for r in rows),
            "candidate_status": [r.get("candidate_status", "") for r in rows],
            "write_barrier_ms": max((r.get("write_barrier_ms", 0) for r in rows), default=0),
            "ready_queue_wait_ms": max((round((began - r.get("ready_at", began)) * 1000) for r in rows), default=0)})
        self.voice_timings[turn_key] = (began, metrics)
        if len(self.voice_timings) > 32:
            self.voice_timings.pop(next(iter(self.voice_timings)))

        def plan_ready(plan):
            nonlocal emitted
            if emitted:
                return
            emitted = True
            metrics['model_to_reply_ms'] = round((monotonic() - began) * 1000)
            metrics['reply_ready_at_ms'] = time() * 1000
            metrics['speech_to_reaction_ms'] = (round(max(0, metrics['reply_ready_at_ms'] - metrics['speech_end_at_ms']))
                                               if metrics['speech_end_at_ms'] is not None else None)
            metrics['received_to_reaction_ms'] = (round(max(0, metrics['reply_ready_at_ms'] - max(r['received_at_ms'] for r in rows)))
                                                 if rows else None)
            # Only enqueue on the event loop. Neither synthesis nor playback
            # blocks cognition or the independent write-zone call.
            valid = {u.speaker.object_id for u in (*envelope.utterances, *envelope.current_utterances)} | {envelope.speaker.object_id}
            items = tuple(ResponseItem(i.channel, i.text, tuple(t for t in (self.object_of(token) for token in i.target_ids) if t in valid))
                          for i in plan.items if i.channel == "verbal" and i.text.strip()
                          and (not i.target_ids or any(self.object_of(t) in valid for t in i.target_ids))) if plan.mode == "respond" else ()
            if not items:
                return
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._start_plan(items, envelope.speaker.object_id, epoch, turn_key)))

        await self.emit("thinking")
        def review_ready(response, incoming):
            nonlocal envelope
            reviewed, envelope, speaker = self.apply_main_review(response, incoming, epoch)
            return reviewed, envelope, speaker
        def experience():
            result = self.process.experience(self.subject_id, envelope.text, envelope=envelope,
                objects={u.speaker.label:u.speaker.object_id for u in (*envelope.utterances, *envelope.current_utterances) if u.speaker.status in {"introduced", "recognized"}},
                resolved_speaker=self.identities.candidate(envelope.speaker), on_voice_plan=plan_ready,
                defer_write=True, code_restore=self.restore_codes,
                action_allowed=lambda: not self.closed and epoch == self.epoch,
                on_conversation_review=review_ready,
                object_codes={oid: code for code, oid in self.code_owner.items()})
            return result, self.process.last_model_response, result.selected_tool_ids

        result, response, selected = await loop.run_in_executor(self.turn_executor, experience)
        job = self.process.take_deferred_write()
        if job is not None:
            # Queued requests are wakeups, not one immutable write per turn.
            self.failed_write_jobs.clear()
            while not self.write_queue.empty():
                self.write_queue.get_nowait()
                self.write_queue.task_done()
            self.write_queue.put_nowait(job)
        self.last_debug = {"activity_id": result.activity.id,
            'input': envelope.to_dict(), 'scene': [asdict(u) for u in envelope.utterances],
            'identity': [asdict(u.speaker) for u in envelope.current_utterances],
            'delivery': self.delivery.snapshot(), 'jev': self.last_jev, 'response': asdict(response),
            "timing": {**(asdict(result.timing) if result.timing else {}), 'voice': metrics},
            "tool": {"selected_tool_ids": selected, "tool_input": result.current_state.tool_input,
                     "tool_hot_state": result.current_state.tool_hot_state,
                     "intent": asdict(response.tool_intent) if response.tool_intent else None,
                     "handling": response.tool_handling, "consumed": response.tool_consumed}}
        self.debug_history.append(self.last_debug)
        self.record('voice_turn_diagnostics', self.last_debug)
        await self.emit('debug_turn', activity_id=result.activity.id, input=envelope.text[:100])
        handling = response.tool_handling
        if not handling and response.tool_consumed and not response.response_plan.unsaid_text():
            handling = tuple({"task_id": tid, "disposition": "answered", "evidence": response.text,
                              "work_complete": True} for tid in response.tool_consumed)
        if handling:
            self.tool_handling[turn_key] = (envelope.speaker.object_id, handling, selected,
                                        response.tool_intent.need if response.tool_intent else "")
            self._confirm_tool_delivery(turn_key)
        if not emitted and result.action_text:
            plan_ready(response.response_plan)

    async def _start_reply(self, text: str, object_id: str, epoch: int) -> None:
        await self._start_plan((ResponseItem("verbal", text, (object_id,)),), object_id, epoch, epoch)

    async def _start_plan(self, items, object_id, epoch, turn_key) -> None:
        if self.closed or epoch != self.epoch:
            return
        if self.delivery.snapshot().get("state") in {"queued", "playing", "paused"}:
            if len(self.pending_plans) == self.pending_plans.maxlen:
                await self.emit("notice", text="待播回应较多，暂不增加新语音。")
                return
            self.pending_plans.append((items, object_id, epoch, turn_key))
            return
        text = "\n".join(i.text for i in items)
        d = self.delivery.begin(text, object_id, items=items)
        self.reply_records[turn_key] = d
        self.reply_epochs[d.reply_id] = turn_key
        # A long-running voice session should not retain every synthesized reply.
        if len(self.reply_records) > 32:
            for old in list(self.reply_records)[:-32]:
                old_reply = self.reply_records.pop(old)
                self.reply_epochs.pop(old_reply.reply_id, None)
                self.tool_handling.pop(old, None)
        if self.pending_voice:
            self.delivery.pause()
        await self.emit("reply", reply_id=d.reply_id, text=text, paused=self.pending_voice,
                        generated_at_ms=self.voice_timings.get(turn_key, (None, {}))[1].get('reply_ready_at_ms'),
                        items=[asdict(i) for i in items])
        self.record("voice_delivery", self.delivery.snapshot())
        self.attention.engage(t for item in items for t in item.target_ids)
        self.tts_task = asyncio.create_task(self._synthesize(d))

    async def _next_plan(self):
        if self.pending_plans and self.delivery.snapshot().get("state") in {"completed", "failed", "stopped"}:
            await self._start_plan(*self.pending_plans.popleft())

    async def _synthesize(self, d) -> None:
        synthesis_started = monotonic()
        try:
            for segment, text in enumerate(d.texts):
                stream = self.cloud.synthesize(text)
                try:
                    async for pcm in stream:
                        if not self.delivery.accepts(d.reply_id):
                            return
                        measured = self.voice_timings.get(self.reply_epochs.get(d.reply_id))
                        if measured and 'first_audio_ms' not in measured[1]:
                            measured[1]['first_audio_ms'] = round((monotonic() - measured[0]) * 1000)
                            measured[1]['synthesis_first_packet_ms'] = round((monotonic() - synthesis_started) * 1000)
                        await self.emit("audio", reply_id=d.reply_id, segment=segment,
                                        text=text, pcm=base64.b64encode(pcm).decode(), rate=self.cloud.config.template.sample_rate)
                finally:
                    await stream.aclose()
                if not self.delivery.accepts(d.reply_id):
                    return
                await self.emit("segment_end", reply_id=d.reply_id, segment=segment)
            self.delivery.generated(d.reply_id)
            self.record("voice_delivery", self.delivery.snapshot())
            self._confirm_tool_delivery(self.reply_epochs.get(d.reply_id))
            await self.emit("audio_end", reply_id=d.reply_id)
            await self._next_plan()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logging.getLogger(__name__).exception("Voice synthesis failed")
            self.delivery.fail(d.reply_id, str(exc))
            self.record("voice_delivery", self.delivery.snapshot())
            self.process.note_scene_delivery(self.subject_id, f"failed:{d.reply_id}",
                "（播放器交付失败）" + json.dumps(self.delivery.snapshot(), ensure_ascii=False)
                + "；partial_text未确认整句播完，pending_text尚未播放。")
            await self.emit("stop", reply_id=d.reply_id)
            await self.emit("notice", text=f"语音合成失败，文字回应已保留：{exc}")
            await self._next_plan()

    async def acknowledge(self, payload: dict) -> None:
        if self.delivery.acknowledge(str(payload.get("reply_id", "")), int(payload.get("segment", -1)), payload.get("phase", "")):
            measured = self.voice_timings.get(self.reply_epochs.get(str(payload.get('reply_id',''))))
            if measured and payload.get('phase') == 'started' and 'playback_start_ms' not in measured[1]:
                measured[1]['playback_start_ms'] = round((monotonic() - measured[0]) * 1000)
            self.record("voice_delivery", self.delivery.snapshot())
            if payload.get("phase") == "completed":
                d = self.delivery.current
                targets = d.targets[int(payload['segment'])] if d.targets else (d.object_id,)
                self.process.note_scene_delivery(
                    self.subject_id, f"played:{d.reply_id}:{payload['segment']}",
                    "（播放器确认已播出）匠石：" + d.texts[int(payload['segment'])],
                )
                self.write_queue.put_nowait(lambda: self.process._flush_pending_scene(self.subject_id))
                self.jev_scene_store.append(f"played:{d.reply_id}:{payload['segment']}",
                    {"p": "匠石", "text": d.texts[int(payload['segment'])], "basis": "已播出", "at": time()*1000,
                     "targets": [self.assign_code(oid, self.code_labels.get(oid, "")) for oid in targets]})
                self.played_history.append({"who": "匠石", "basis": "已播出", "text": d.texts[int(payload['segment'])],
                                            "targets": list(targets), "at": time() * 1000})
                self.interaction_feedback.played(d.reply_id, int(payload['segment']), d.texts[int(payload['segment'])],
                    targets, self.reply_input_ids.get(self.reply_epochs.get(d.reply_id), ()))
                if len(targets) == 1:
                    self.identities.name_question_played(targets[0], d.texts[int(payload['segment'])])
                ledger = getattr(self.process, "activity_ledger", None)
                if ledger is not None:
                    ledger.append_subject_reply(self.subject_id,
                        text_raw="（播放器确认此句播放完成）" + d.texts[int(payload["segment"])],
                        mentioned_object_ids=d.targets[int(payload["segment"])] if d.targets else (d.object_id,),
                        objects={t:t for t in (d.targets[int(payload["segment"])] if d.targets else (d.object_id,))},
                        response_statuses=("verbal",), state_delta={"voice_reply_id": d.reply_id, "delivery": "played"})
                self._confirm_tool_delivery(self.reply_epochs.get(d.reply_id))
                await self._next_plan()

    def _confirm_tool_delivery(self, epoch) -> None:
        d = self.reply_records.get(epoch)
        pending = self.tool_handling.get(epoch)
        if d is None or d.state != "completed" or pending is None:
            return
        object_id, handling, selected, need = pending
        service = getattr(self.process, "tool_service", None)
        if service is not None:
            service.handle_results(self.subject_id, object_id, handling, selected_ids=selected,
                reply=d.snapshot()["played_text"], unsaid="", new_need=need)
        self.tool_handling.pop(epoch, None)

    async def close(self, *, on_progress=None) -> None:
        if self.closed:
            return
        def progress(stage):
            if on_progress is not None:
                on_progress(stage)
        progress('停止播放与入口')
        await self.stop("connection closed")
        self.closed = True
        if self.background_tasks:
            progress('等待登记或诊断任务结束')
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        self.input_records.clear()
        self.annotations.clear()
        self.worker.cancel()
        self.turn_worker.cancel()
        self.person_review_worker.cancel()
        await asyncio.gather(self.worker, self.turn_worker, self.person_review_worker, return_exceptions=True)
        self.person_review_executor.shutdown(wait=False, cancel_futures=True)
        if self.tts_task:
            await asyncio.gather(self.tts_task, return_exceptions=True)
        # Running synchronous cognition finishes its write; stale callbacks are
        # suppressed. Python cannot forcibly cancel a running HTTP request.
        progress('等待已启动的主认知结束并保存')
        await asyncio.to_thread(self.turn_executor.shutdown, wait=True, cancel_futures=True)
        progress('保存待写片场')
        try:
            await asyncio.wait_for(self.write_queue.join(), 60)
        except asyncio.TimeoutError:
            logging.getLogger(__name__).warning("写场在关闭时尚未完成")
        self.write_worker.cancel()
        await asyncio.gather(self.write_worker, return_exceptions=True)
        await asyncio.to_thread(self.write_executor.shutdown, wait=True, cancel_futures=True)
        # A timed-out JEV request only produces a discarded judgment. It does
        # not commit cognition/scene state, so reconnection need not await it.
        self.jev_executor.shutdown(wait=False, cancel_futures=True)
        progress('等待声音参考任务结束')
        await asyncio.to_thread(self.candidate_executor.shutdown, wait=True, cancel_futures=True)
        await asyncio.to_thread(self.sample_executor.shutdown, wait=True, cancel_futures=True)
        if self.sample_jobs:
            await asyncio.gather(*self.sample_jobs, return_exceptions=True)


def create_app(process, subject_id: str, config: VoiceConfig, jev=None, local=None, local_tts=None, online_speakers=None, *, speaker_models=None, trial_root=None):
    from aiohttp import web, ClientSession, ClientTimeout, WSMsgType

    visual = getattr(process, 'vision', None)
    app = web.Application(client_max_size=max(2 * 1024 * 1024, visual.config.max_image_bytes if visual else 0))
    busy = False
    cleanup_stage = ""
    registry_busy = False
    text_write_task = None
    pending_text_write = None
    text_write_error = ''
    active_conversation = None
    environment_pending = None

    async def deliver_environment(environment, decision):
        nonlocal registry_busy, environment_pending
        environment_pending = environment
        while registry_busy or (busy and active_conversation is None):
            await asyncio.sleep(.1)
        environment, environment_pending = environment_pending, None
        if environment is None:
            return
        envelope = InputEnvelope(session_id='visual-environment', source='visual_environment',
            parts=(InputPart('text', '【环境观察变化或观察结果，不是人物发言】\n' + environment['description']),
                   InputPart('image', reference=environment['frame'], media_type='image/jpeg')),
            speaker=SpeakerEvidence('environment', label='环境观察', status='unknown', method='visual_scene'),
            visual_snapshot_id=environment['id'], deferred_interrupt=True)
        observation = SceneUtterance(envelope.text, envelope.speaker, 0, 0, input_id=envelope.input_id,
                                      received_at_ms=time()*1000, recorded_start_at_ms=environment['captured']*1000,
                                      recorded_end_at_ms=environment['captured']*1000, identity_note='视觉观察，不是人物发言')
        envelope = replace(envelope, current_utterances=(observation,))
        if active_conversation is not None:
            active_conversation.enqueue_turn(envelope)
        else:
            registry_busy = True
            try:
                envelope = await judge_standalone(envelope)
                await asyncio.to_thread(process.experience, subject_id, envelope.text, envelope=envelope,
                    processing_modes=('environment',), defer_write=True)
                schedule_text_scene()
            finally:
                registry_busy = False

    async def judge_standalone(envelope):
        from jshi.voice.jev_scene import JEVSceneStore
        import hashlib
        scene_store = JEVSceneStore(process.repository.path.parent / 'jev_scenes' / hashlib.sha256(subject_id.encode()).hexdigest()[:16] / 'text-entry.json')
        scene, unwritten = scene_store.view(time()*1000)
        envelope = pack_input(envelope, visual, subject_id)
        judge = jev or VoiceJEV()
        def capture(request):
            process._record_step_input(request, judge.model, subject_id=subject_id, activity_id='jev-'+envelope.input_id)
        result = await asyncio.to_thread(judge.decide_envelope, subject_id, envelope,
            {'state':'completed', 'jev_scene':scene, 'unwritten':unwritten, 'now':time()*1000}, vision=visual, on_request=capture)
        record = {'main_prompt_hint':result.main_prompt_hint, 'judged':result.judged,
                  'model_error':result.model_error, 'items':[asdict(i) for i in result.items],
                  'input_ids':[envelope.input_id]}
        scene_store.append(envelope.input_id, {'text':envelope.text, 'at':time()*1000,
                                              'source':envelope.source, 'basis':'统一输入信封'})
        utterance = SceneUtterance(envelope.text, envelope.speaker, 0, 0, input_id=envelope.input_id,
                                    received_at_ms=time()*1000, identity_note='统一信封来源：'+envelope.source)
        return replace(envelope, jev_calls=(record,), current_utterances=(utterance,))

    async def flush_text_scene():
        nonlocal pending_text_write, text_write_error
        while pending_text_write is not None:
            write = pending_text_write
            pending_text_write = None
            try:
                ok = await asyncio.to_thread(write)
                text_write_error = '' if ok is not False else '写场未完成，材料仍保留待重试'
            except Exception as exc:
                text_write_error = str(exc)
                logging.getLogger(__name__).exception('Background text scene write failed')

    def schedule_text_scene():
        nonlocal pending_text_write, text_write_task
        write = process.take_deferred_write()
        if write is None:
            return
        # A flush reads all pending turns, so coalesce wakeups while one is running.
        pending_text_write = write
        if text_write_task is None or text_write_task.done():
            text_write_task = asyncio.create_task(flush_text_scene())

    async def finish_text_writes(app):
        if text_write_task is not None:
            await asyncio.shield(text_write_task)

    app.on_cleanup.append(finish_text_writes)
    self_tests = {}
    trial = None
    if speaker_models and trial_root:
        from jshi.voice.comparison import SpeakerTrial
        trial = SpeakerTrial(trial_root, speaker_models)

    async def page(request):
        return web.FileResponse(Path(__file__).with_name("voice_ui.html"))

    async def worklet(request):
        return web.FileResponse(Path(__file__).with_name("voice_capture.js"))

    async def enrollment_page(request):
        return web.FileResponse(Path(__file__).with_name('voice_enroll.html'))

    async def registry(request):
        nonlocal registry_busy
        if request.method == 'GET':
            try:
                speakers, _ = await registry_speakers(request.query.get('model', ''))
                counter = getattr(speakers, 'saved_reference_counts', None)
                counts = {r['object_id']: r['reference_count'] for r in counter()} if callable(counter) else None
            except ValueError:
                counts = None
            return web.json_response({'models': list(speaker_models or {}), 'people': [
                {'object_id': p.object_id, 'label': p.label, 'voiceprints': len([c for c in p.carriers if c.kind == 'voiceprint']),
                 'reference_count': counts.get(p.object_id, 0) if counts is not None else None}
                for p in process.profiles.list() if p.status != 'rejected' and
                (p.source.startswith('voice_') or any(c.kind == 'voiceprint' for c in p.carriers))]},
                headers={'Cache-Control': 'no-store'})
        if request.headers.get('Origin', '') != f'http://{request.host}':
            raise web.HTTPForbidden(text='登记操作需要同源请求')
        if busy or registry_busy:
            raise web.HTTPConflict(text='请先结束语音对话，再登记或修改姓名')
        registry_busy = True
        try:
            payload = await request.json()
            action = payload.get('action')
            if action in {'try', 'summary', 'clear_trials', 'threshold'}:
                return web.json_response(await self_test(action, payload))
            name = str(payload.get('name', '')).strip()
            if not name or len(name) > 40 or any(ord(c) < 32 for c in name):
                raise ValueError('请填写 1–40 字的姓名')
            object_id = str(payload.get('object_id', ''))
            if action == 'rename':
                profile = process.profiles.rename(object_id, name, expected_label=str(payload.get('previous_name', '')))
                process.repository.add_history(HistoryRecord(subject_id=subject_id, kind=HistoryKind.FACT,
                    event_type='voice_identity_rename', content={'object_id': object_id,
                    'previous_name': payload.get('previous_name'), 'name': name, 'basis': 'manual_text_correction'}, source_ids=()))
                return web.json_response({'object_id': object_id, 'label': profile.label})
            if action != 'enroll':
                raise ValueError('未知登记操作')
            clips = payload.get('clips')
            if not isinstance(clips, list) or not 1 <= len(clips) <= 8:
                raise ValueError('请选择 1–8 段录音')
            pcm = [base64.b64decode(c, validate=True) for c in clips]
            if any(len(c) % 2 or not 96000 <= len(c) <= 960000 for c in pcm) or sum(map(len, pcm)) > 1200000:
                raise ValueError('每段须为 3–30 秒的单声道 16k PCM；总录音不超过 37 秒')
            speakers, _ = await registry_speakers(str(payload.get('model', '')))
            if object_id:
                profile = process.profiles.get(object_id)
                if profile is None or profile.status == 'rejected' or profile.label != name:
                    raise ValueError('所选对象已变化，请刷新；修改姓名请用更名功能')
            else:
                ids = VoiceIdentities(process.profiles, subject_id, 'standalone-enrollment')
                evidence, _ = ids.introduce('manual-'+uuid4().hex, name, basis='manual_selection')
                if evidence.status != 'introduced':
                    raise ValueError('存在多个同名对象，请选择具体对象')
                object_id = evidence.object_id
            stats = await asyncio.to_thread(speakers.enroll_samples, pcm, object_id, user_labeled=True)
            return web.json_response({'object_id': object_id, 'label': name, **stats})
        except (ValueError, KeyError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        finally:
            registry_busy = False

    async def registry_speakers(model):
        if model:
            if not speaker_models or model not in speaker_models:
                raise ValueError('未知声纹模型')
            speakers = await asyncio.to_thread(speaker_models[model])
        else:
            speakers = online_speakers or (local.speakers if local else None)
        if speakers is None:
            raise ValueError('本地声纹模型尚未加载')
        from jshi.voice.selftest import SelfTest
        path = speakers.path.with_name(speakers.path.stem + '.selftest.json')
        if path not in self_tests:
            self_tests[path] = SelfTest(path)
        return speakers, self_tests[path]

    async def self_test(action, payload):
        speakers, trials = await registry_speakers(str(payload.get('model', '')))
        if action == 'threshold':
            await asyncio.to_thread(speakers.set_threshold, payload.get('value'))
        elif action == 'clear_trials':
            trials.clear()
        result = {}
        if action == 'try':
            pcm = base64.b64decode(str(payload.get('clip', '')), validate=True)
            if len(pcm) % 2 or not 48000 <= len(pcm) <= 960000:
                raise ValueError('试听录音须为 1.5–30 秒')
            import numpy as np
            samples = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
            vector = await asyncio.to_thread(speakers.embedding, samples)
            if vector is None:
                raise ValueError('这段录音提取不到声纹，请靠近麦克风再说一次')
            ranking = speakers.rank_vector(vector)
            from jshi.voice.selftest import decide
            matched = decide(ranking, speakers.confirmation_threshold, speakers.margin)
            expected = str(payload.get('expected', ''))
            if expected and process.profiles.get(expected) is None:
                raise ValueError('所选人物不存在，请刷新')
            trials.record(expected, vector)
            def label(object_id):
                profile = process.profiles.get(object_id)
                return profile.label if profile else object_id
            result = {'expected': expected, 'matched': matched, 'matched_label': label(matched) if matched else '',
                      'ranking': [{'object_id': o, 'label': label(o), 'score': round(s, 3)} for s, o in ranking[:3]]}
        return {**result, 'summary': await asyncio.to_thread(trials.summary, speakers)}

    async def status(request):
        return web.json_response({"busy": busy, "cleanup_stage": cleanup_stage, "asr": "local" if local is not None else config.asr_backend,
            'text_scene_writing': text_write_task is not None and not text_write_task.done(),
            'text_scene_write_error': text_write_error,
            'speaker_models': list(speaker_models or {}),
            "local_available": local is not None, "online_available": bool(config.api_key), "vision_available": visual is not None, "voice_active": active_conversation is not None},
            headers={"Cache-Control": "no-store"})

    async def text_people(request):
        return web.json_response({'people': [{'object_id': p.object_id, 'label': p.label}
            for p in process.profiles.list() if p.status != 'rejected' and p.source != 'voice_scene']},
            headers={'Cache-Control': 'no-store'})

    async def text_input(request):
        nonlocal registry_busy
        if request.headers.get('Origin', '') != f'http://{request.host}':
            raise web.HTTPForbidden(text='文字输入需要同源请求')
        if busy or registry_busy:
            raise web.HTTPConflict(text='正在处理其他会话。语音连接时请在当前页面发送文字；或结束语音后再试。')
        try:
            payload = await request.json()
            text = payload.get('text', '').strip()
            profile = process.profiles.get(payload.get('object_id', ''))
        except (ValueError, AttributeError, TypeError):
            raise web.HTTPBadRequest(text='文字输入格式有误')
        if not text or len(text) > 2000 or any(ord(c)<32 and c not in '\n\t' for c in text):
            raise web.HTTPBadRequest(text='请输入1–2000个字')
        if profile is None or profile.status == 'rejected' or profile.source == 'voice_scene':
            raise web.HTTPBadRequest(text='请先选择一个有效人物')
        if busy or registry_busy:
            raise web.HTTPConflict(text='正在处理其他会话，请稍后再发送')
        registry_busy = True
        try:
            evidence = SpeakerEvidence('typed:'+profile.object_id, profile.object_id, profile.label,
                                       'recognized', 'user_selected', 1.0)
            envelope = InputEnvelope(session_id='web-text', source='web_text',
                parts=(InputPart('text', text),), speaker=evidence)
            streaming = 'application/x-ndjson' in request.headers.get('Accept', '')
            events = asyncio.Queue()
            loop = asyncio.get_running_loop()
            def plan_ready(plan):
                loop.call_soon_threadsafe(events.put_nowait, {'type': 'reply',
                    'reply': plan.verbal_text(), 'mode': plan.mode, 'label': profile.label})
            response = None
            if streaming:
                response = web.StreamResponse(headers={'Content-Type': 'application/x-ndjson; charset=utf-8',
                    'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
                await response.prepare(request)
                await response.write((json.dumps({'type':'accepted','label':profile.label},ensure_ascii=False)+'\n').encode())
            async def run_turn():
                try:
                    judged_envelope = await judge_standalone(envelope)
                    return await asyncio.to_thread(process.experience, subject_id, text, envelope=judged_envelope,
                        objects={profile.label:profile.object_id}, on_voice_plan=plan_ready if streaming else None,
                        defer_write=True)
                finally:
                    schedule_text_scene()
            job = asyncio.create_task(run_turn())
            if streaming:
                job.add_done_callback(lambda _: events.put_nowait({'type':'finished'}))
                try:
                    while True:
                        event = await events.get()
                        if event['type']=='finished':
                            break
                        await response.write((json.dumps(event,ensure_ascii=False)+'\n').encode())
                    result = await asyncio.shield(job)
                    event = {'type':'complete','reply':result.response_plan.verbal_text(),
                        'mode':result.response_plan.mode,'label':profile.label,
                        'scene_write':'background',
                        'activity_id':str(getattr(result,'activity_id','')),
                        'timing':asdict(result.timing) if result.timing else None}
                    await response.write((json.dumps(event,ensure_ascii=False,default=str)+'\n').encode())
                    await response.write_eof()
                except (ConnectionError, asyncio.CancelledError):
                    await job  # Losing the page must not abandon this turn's writes.
                    raise
                except Exception as exc:
                    logging.getLogger(__name__).exception('Web text processing failed')
                    await response.write((json.dumps({'type':'error','message':str(exc)},ensure_ascii=False)+'\n').encode())
                    await response.write_eof()
                return response
            try:
                result = await asyncio.shield(job)
            except asyncio.CancelledError:
                await job  # Keep the shared process reserved until its writes finish.
                raise
            reply = result.action_text or ''
            return web.json_response({'reply': reply, 'mode': result.response_plan.mode,
                'scene_write': 'background',
                'label': profile.label, 'activity_id': str(getattr(result, 'activity_id', ''))},
                headers={'Cache-Control': 'no-store'})
        finally:
            registry_busy = False

    async def connection(request):
        nonlocal busy, cleanup_stage, active_conversation
        origin = request.headers.get("Origin", "")
        if origin != f"http://{request.host}":
            raise web.HTTPForbidden(text="voice connection requires same origin")
        if busy or registry_busy:
            raise web.HTTPConflict(text="已有一个语音会话，请先结束它。")
        backend = request.query.get("asr", "local" if local is not None else config.asr_backend)
        if backend not in {"local", "online"}:
            raise web.HTTPBadRequest(text="未知语音识别后端")
        use_local = local if backend == "local" else None
        if backend == "local" and use_local is None:
            raise web.HTTPBadRequest(text="本地识别未加载，请用 --asr local 启动")
        if backend == "online" and not config.api_key:
            raise web.HTTPBadRequest(text="线上识别缺少 JSHI_VOICE_API_KEY，配置后重启服务")
        busy = True
        cleanup_stage = ""
        ws = web.WebSocketResponse(max_msg_size=128 * 1024, heartbeat=20)
        conversation = None
        receiver = None
        asr = None
        local_executor = None
        cloud = None
        audio = SessionAudio()
        try:
            await ws.prepare(request)
            async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=15, sock_read=60)) as http:
                cloud = VolcVoice(replace(config, asr_backend=backend), http)
                speakers = use_local.speakers if use_local else (online_speakers or (local.speakers if local else None))
                selected_model = request.query.get('speaker_model', '')
                if selected_model:
                    if not speaker_models or selected_model not in speaker_models:
                        raise ValueError('所选声纹模型不可用')
                    speakers = await asyncio.to_thread(speaker_models[selected_model])
                    if speakers is None:
                        raise ValueError('所选声纹模型尚未准备')
                    if use_local:
                        use_local.speakers = speakers
                        if getattr(use_local, 'diarizer', None):
                            await asyncio.to_thread(use_local.diarizer.use, selected_model, speakers)
                if speakers is not None:
                    speakers.tracks.clear()
                    getattr(speakers, 'track_samples', {}).clear()
                    getattr(speakers, 'pending_tracks', []).clear()
                    speakers.last_embedding.clear()
                    speakers.binding.clear()
                    if hasattr(speakers, 'reset_session'):
                        speakers.reset_session()
                conversation = VoiceConversation(process, subject_id, local_tts or cloud, ws.send_json, jev=jev,
                    timeout_s=config.jev_timeout_s, local_speakers=speakers, input_pause_s=config.input_pause_s, sample_source=audio,
                    candidate_timeout_s=config.candidate_timeout_s, session_id=request.query.get("session") or None)
                active_conversation = conversation
                if use_local is not None:
                    use_local.reset_session()
                    local_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jshi-local-asr")
                else:
                    asr = await cloud.open_asr()
                    if speakers is not None:
                        local_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jshi-cloud-identity")
                assembler = TranscriptAssembler(identity_mode=config.identity_mode)

                async def receive():
                    failed = False
                    final_received = False
                    def closed_reason():
                        detail = asr.exception() if hasattr(asr, 'exception') else None
                        detail_text = f'{type(detail).__name__}: {detail}' if detail is not None else '远端未提供关闭详情'
                        return f"ASR connection closed (code={getattr(asr, 'close_code', None)}, detail={detail_text})"
                    try:
                        async for message in asr:
                            if message.type == WSMsgType.BINARY:
                                payload, final = unpack_asr(message.data)
                                for transcript in assembler.accept(payload):
                                    if speakers is not None:
                                        transcript = await asyncio.get_running_loop().run_in_executor(
                                            local_executor, audio.match, transcript, speakers)
                                    await conversation.accept(transcript)
                                if final:
                                    final_received = True
                                    break
                            elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSED, WSMsgType.CLOSE}:
                                raise RuntimeError(closed_reason())
                        if not final_received and not ws.closed:
                            raise RuntimeError(closed_reason())
                    except Exception as exc:
                        failed = True
                        logging.getLogger(__name__).exception("Voice ASR receive failed")
                        conversation.record('voice_asr_failure', {'error': str(exc),
                            'error_type': type(exc).__name__, 'close_code': getattr(asr, 'close_code', None)})
                        await conversation.emit("notice", text=f"语音识别失败：{exc}")
                    finally:
                        if not ws.closed:
                            await ws.close(code=1011 if failed else 1000,
                                           message=b'ASR upstream disconnected' if failed else b'')

                if asr is not None:
                    receiver = asyncio.create_task(receive())
                await conversation.emit("ready", session_id=conversation.session_id, identity_mode="local" if use_local else config.identity_mode,
                    voiceprint_available=speakers is not None,
                    voiceprint_count=len(getattr(speakers, 'known', {})) if speakers else 0,
                    saved_references=getattr(speakers, 'saved_reference_counts', lambda: [])() if speakers else [],
                    match_threshold=getattr(speakers, 'threshold', None),
                    single_confirmation_threshold=getattr(speakers, 'confirmation_threshold', None),
                    match_margin=getattr(speakers, 'margin', None),
                    comparison_available=trial is not None,
                    names=[p.label for p in process.profiles.list() if p.status != 'rejected' and p.source != 'voice_scene'][:100],
                    asr="本地说话人分段 + 整句识别" if use_local and getattr(use_local, "diarizer", None) else ("本地流式 + 整句复核" if use_local and getattr(use_local, "refiner", None) else ("本地小模型流式" if use_local else "火山流式 + 整句复核 + 多人时间线")),
                    tts="本地固定音色" if local_tts else ("火山双向流式合成" if config.tts_transport == 'websocket' else "在线 SSE 合成"))
                async for message in ws:
                    if message.type == WSMsgType.BINARY:
                        if len(message.data) > 32000 or len(message.data) % 2:
                            raise ValueError("invalid microphone PCM frame")
                        audio.append(message.data)
                        if use_local is not None:
                            transcripts = await asyncio.get_running_loop().run_in_executor(local_executor, use_local.feed, message.data)
                            for transcript in transcripts:
                                await conversation.accept(transcript)
                        else:
                            await asr.send_bytes(pack_asr(message.data, audio=True))
                    elif message.type == WSMsgType.TEXT:
                        payload = message.json()
                        kind = payload.get("type")
                        if kind == "speech_start":
                            pass  # Sound energy alone never interrupts playback.
                        elif kind == "noise":
                            await conversation.noise()
                        elif kind == "stop":
                            await conversation.stop()
                        elif kind == "retry_write":
                            await conversation.retry_failed_writes()
                        elif kind == "debug":
                            task = asyncio.create_task(conversation.diagnostics(str(payload.get("section", "")), activity_id=str(payload.get('activity_id','')),
                                part=str(payload.get('part','all')), purpose=str(payload.get('purpose','all'))))
                            conversation.background_tasks.add(task)
                            task.add_done_callback(conversation.background_tasks.discard)
                        elif kind in {'compare_add', 'compare_run', 'compare_new'}:
                            async def compare(payload=payload):
                                try:
                                    if trial is None: raise ValueError('当前服务未配置声纹对照')
                                    if payload['type'] == 'compare_add':
                                        ids = payload.get('input_ids')
                                        if not isinstance(ids, list) or not 1 <= len(ids) <= 32: raise ValueError('请选择 1–32 段声音')
                                        rows = [conversation.input_records.get(str(i)) for i in dict.fromkeys(ids)]
                                        if any(r is None for r in rows): raise ValueError('部分声音已过期')
                                        summary = await asyncio.to_thread(trial.add, payload.get('name', ''), payload.get('role'), rows, conversation.session_id)
                                        await conversation.emit('comparison_samples', input_ids=ids, **summary)
                                    elif payload['type'] == 'compare_new':
                                        summary = await asyncio.to_thread(trial.new_group)
                                        await conversation.emit('comparison_samples', **summary)
                                    else:
                                        await conversation.emit('comparison_pending')
                                        report = await asyncio.to_thread(trial.run)
                                        await conversation.emit('comparison_report', report=report, path=str(trial.root.resolve()))
                                except Exception as exc:
                                    await conversation.emit('comparison_error', text=str(exc))
                            task = asyncio.create_task(compare())
                            conversation.background_tasks.add(task)
                            task.add_done_callback(conversation.background_tasks.discard)
                        elif kind == 'voice_settings':
                            try:
                                if speakers is None: raise ValueError('本地声纹模型尚未加载')
                                threshold = await asyncio.to_thread(speakers.set_threshold, payload.get('match_threshold'))
                                await conversation.emit('voice_settings', match_threshold=threshold,
                                    single_confirmation_threshold=speakers.confirmation_threshold, match_margin=speakers.margin)
                            except Exception as exc:
                                await conversation.emit('voice_settings_error', text=str(exc), match_threshold=getattr(speakers, 'threshold', None),
                                    single_confirmation_threshold=getattr(speakers, 'confirmation_threshold', None))
                        elif kind == 'text':
                            await conversation.typed(payload.get('text', ''), payload.get('object_id', ''))
                        elif kind == 'rename_confirm':
                            await conversation.apply_rename(str(payload.get('object_id', '')), str(payload.get('name', '')), bool(payload.get('accept')))
                        elif kind == 'enroll_inputs':
                            task = asyncio.create_task(conversation.enroll_inputs(payload.get('input_ids'), payload.get('name', '')))
                            conversation.background_tasks.add(task)
                            task.add_done_callback(conversation.background_tasks.discard)
                        elif kind == "played":
                            await conversation.acknowledge(payload)
                        elif kind == "end":
                            if asr is not None:
                                await asr.send_bytes(pack_asr(b"", audio=True, final=True))
                            break
                    elif message.type == WSMsgType.ERROR:
                        raise RuntimeError(f"browser connection: {ws.exception()}")
        except Exception as exc:
            logging.getLogger(__name__).exception("Voice connection failed")
            if not ws.closed:
                await ws.send_json({"type": "notice", "text": f"语音连接失败：{type(exc).__name__}: {exc}"})
        finally:
            def closing(stage):
                nonlocal cleanup_stage
                cleanup_stage = stage
            async def cleanup():
                if receiver:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
                actions = []
                if asr:
                    actions.append(('关闭语音识别连接', asr.close))
                if conversation:
                    actions.append(('保存本轮状态', lambda: conversation.close(on_progress=closing)))
                if cloud is not None and hasattr(cloud, 'close'):
                    actions.append(('关闭声音服务', cloud.close))
                if local_executor:
                    actions.append(('等待本地声音处理结束', lambda: asyncio.to_thread(local_executor.shutdown, wait=True, cancel_futures=True)))
                actions.append(('关闭浏览器连接', ws.close))
                for stage, action in actions:
                    closing(stage)
                    try:
                        await action()
                    except Exception:
                        logging.getLogger(__name__).exception('Voice cleanup failed: %s', stage)
            try:
                await cleanup()
            finally:
                active_conversation = None
                busy = False
                cleanup_stage = ""
        return ws

    app.router.add_get("/", page)
    app.router.add_get("/voice_capture.js", worklet)
    app.router.add_get("/status", status)
    app.router.add_get('/text-people', text_people)
    app.router.add_post('/text-input', text_input)
    from jshi.app.web_data import register_data_routes
    register_data_routes(app, process, subject_id)
    app.router.add_get("/voice", connection)
    app.router.add_get('/voice-enroll', enrollment_page)
    app.router.add_get('/voice-registry', registry)
    app.router.add_post('/voice-registry', registry)
    if getattr(process, 'vision', None):
        from jshi.vision.web import install_vision
        install_vision(app, process, subject_id, deliver_environment, voice_active=lambda: active_conversation is not None)
        visual.scene_context = lambda: active_conversation.jev_scene_store.view(time()*1000)[0] if active_conversation else ''
    return app


def run_voice(process, identities, args) -> None:
    from jshi.app.talk_session import load_session
    try:
        from aiohttp import web
    except ImportError:
        raise SystemExit('语音入口需要安装：python -m pip install -e ".[voice]"')
    config = VoiceConfig.from_env(asr=args.asr, tts=args.tts)
    subject_id = args.subject_id or load_session(args.data_dir).get("subject_id")
    if not subject_id:
        raise SystemExit("请指定主体 id，或先用 talk 建立一次会话。")
    identities.get(subject_id)
    from jshi.core.skillconfig import SkillConfigStore, build_model_port
    from jshi.app.cli import _skills_config_path
    store = SkillConfigStore()
    path = _skills_config_path()
    if path:
        store.load_file(path)
    jev = VoiceJEV(build_model_port(store.profile("voice_jev")))
    local = None
    if config.asr_backend == "local":
        from jshi.voice.local import build_local
        local = build_local(args.data_dir, process.profiles)
    local_tts = None
    if config.tts_backend == "local":
        from jshi.voice.local_tts import build_local_tts
        local_tts = build_local_tts(args.data_dir, config)
    online_speakers = None
    if local is None:
        from jshi.voice.local import build_speakers
        online_speakers = build_speakers(args.data_dir, process.profiles)
    from jshi.voice.local import build_speakers
    model_cache = {}
    primary = online_speakers or (local.speakers if local else None)
    if primary:
        model_cache['eres2netv2' if primary.path.name == 'voiceprints-eres2netv2.json' else 'cam++'] = primary
    def speaker_factory(name):
        def load():
            if name not in model_cache:
                model_cache[name] = build_speakers(args.data_dir, process.profiles, model=name)
            return model_cache[name]
        return load
    models = {name: speaker_factory(name) for name in ('cam++', 'eres2netv2')}
    print(f"语音入口：http://127.0.0.1:{args.port}（选择 LARK A2，输出为电脑默认扬声器）")
    web.run_app(create_app(process, subject_id, config, jev, local, local_tts, online_speakers,
        speaker_models=models, trial_root=args.data_dir/'voice_trials'/'current'), host="127.0.0.1", port=args.port)


def enroll_voice(process, args) -> None:
    async def enroll():
        from aiohttp import ClientSession, ClientTimeout
        config = VoiceConfig.from_env()
        if not config.api_key:
            raise ValueError("在线声纹注册需要 JSHI_VOICE_API_KEY")
        profile = process.profiles.get(args.object_id)
        if profile is None:
            raise ValueError("对象不存在，请使用已有 lux 等对象的 id")
        async with ClientSession(timeout=ClientTimeout(total=60)) as http:
            vpid = await VolcVoice(config, http).register(profile.object_id, args.audio_url)
        attach_voiceprint(process.profiles, profile.object_id, vpid)
        print(f"声纹已关联：{profile.label}（{profile.object_id}）")
    asyncio.run(enroll())
