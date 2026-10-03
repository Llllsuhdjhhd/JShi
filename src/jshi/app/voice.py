"""Local microphone UI; speech credentials never leave the Python server."""
from __future__ import annotations

import asyncio
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
from jshi.models import ResponseItem, ResponsePlan
from jshi.recognition import ObjectProfile, new_object_id
from jshi.subject.domain import HistoryKind, HistoryRecord
from jshi.voice.config import VoiceConfig
from jshi.voice.attention import ConversationAttention
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.delivery import DeliveryTracker
from jshi.voice.identity import VoiceIdentities, attach_voiceprint
from jshi.voice.jev import InterruptDecision, VoiceJEV
from jshi.voice.volc import TranscriptAssembler, VolcVoice, pack_asr, unpack_asr


class VoiceConversation:
    def __init__(self, process, subject_id: str, cloud, send, *, jev=None, timeout_s=3.0, local_speakers=None, input_pause_s=2.0, sample_source=None) -> None:
        self.process, self.subject_id, self.cloud, self.send = process, subject_id, cloud, send
        self.session_id = uuid4().hex
        self.identities = VoiceIdentities(process.profiles, subject_id, self.session_id, process.repository)
        self.delivery = DeliveryTracker()
        previous = [r for r in process.repository.list_history(subject_id, HistoryKind.FACT, 100)
                    if r.event_type == "voice_delivery" and r.content.get("reply_id")]
        if previous:
            self.delivery.previous = dict(previous[-1].content)
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
        self.jev_future = None
        self.active_turn = None
        self.scene = deque(maxlen=24)
        self.pending_plans = deque(maxlen=32)
        self.turn_sequence = 0
        self.last_debug = None
        self.scene_id = new_object_id()
        self.attention = ConversationAttention()
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
        if section not in {"prompt", "timing", "tool", "input", "scene", "identity", "delivery", "jev", "response"}:
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
                calls = [r for r in rows if r.get("kind") == "call" and r.get("activity_id") == activity_id
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
                return '\n\n'.join(rendered) or '本轮没有该调用的提示词记录。', list(dict.fromkeys(sections))
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
            selected = []
            for iid in dict.fromkeys(input_ids):
                row = self.input_records.get(str(iid))
                if row is None or not row['pcm']:
                    raise ValueError('所选声音已过期或没有音频，请选择近期发言')
                if row['transcript'].overlap:
                    raise ValueError('所选发言含重叠声音，请选择清晰、单人发言')
                selected.append(row)
            evidence, _ = self.identities.introduce('manual-' + uuid4().hex, name, basis='manual_selection')
            if evidence.status != 'introduced':
                raise ValueError('这个姓名对应多个对象，请使用唯一的姓名或别名')
            stats = await asyncio.to_thread(self.local_speakers.enroll_samples, [r['pcm'] for r in selected], evidence.object_id)
            for row in selected:
                cluster = row['transcript'].speaker_cluster_id
                if cluster:
                    old = self.identities.resolve(row['transcript'].track_id, cluster_id=cluster)
                    self.identities.associations[old.object_id] = evidence
                    self.identities.clusters[cluster] = evidence
            for iid, row in zip(dict.fromkeys(input_ids), selected):
                who = replace(evidence, track_id=row['transcript'].track_id, status='recognized', method='manual_annotation')
                self.annotations[iid] = who
            self.scene = deque((replace(u, speaker=self.identities.associated(self.annotations.get(u.input_id, u.speaker))) for u in self.scene), maxlen=24)
            self.record('voice_manual_enrollment', {'input_ids': input_ids, 'object_id': evidence.object_id, 'label': evidence.label, **stats})
            await self.emit('enrollment', input_ids=input_ids, object_id=evidence.object_id, label=evidence.label, **stats)
        except Exception as exc:
            ids = list(dict.fromkeys(input_ids)) if isinstance(input_ids, list) else []
            problem_ids = [ids[i] for i in getattr(exc, 'sample_indices', ()) if i < len(ids)]
            await self.emit('enrollment_error', text=str(exc), problem_input_ids=problem_ids)

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

    async def accept(self, transcript) -> None:
        self.last_input_at = asyncio.get_running_loop().time()
        await self.emit("transcript", **asdict(transcript))
        if not transcript.final:
            if not transcript.track_id.startswith("pending-"):
                evidence = self.identities.resolve(transcript.track_id, voiceprint_id=transcript.voiceprint_id, confidence=transcript.confidence, cluster_id=transcript.speaker_cluster_id, uncertain=transcript.identity_uncertain)
                await self.emit("speaker", **asdict(evidence))
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
        self.input_idle.clear()
        await self.queue.put(transcript)

    @staticmethod
    def unfinished(text):
        # A small conservative cue, not a claim to understand every utterance.
        text = text.strip().rstrip('，。！？,.!?… ')
        return text.endswith(('还有一种问题是', '问题是', '就是说', '比如说', '比方说', '然后', '因为', '如果', '但是', '的话', '不需要', '我想', '我觉得'))

    @staticmethod
    def batch_text(utterances) -> str:
        return "\n".join(f"{u.speaker.label} [对象={u.speaker.object_id or '未知'}；轨迹={u.speaker.track_id}；"
                         f"{u.start_ms}–{u.end_ms}ms]：{u.text}" for u in utterances)

    def merge_envelopes(self, envelopes):
        envelope = envelopes[-1]
        pending = [u for previous in envelopes[:-1] for u in previous.current_utterances]
        if pending:
            current = tuple(sorted((*pending, *envelope.current_utterances), key=lambda u: (u.start_ms, u.end_ms)))
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
                parts=(InputPart('text', self.batch_text(current)), InputPart('audio',
                    reference=f'session:{self.session_id}:{start}-{end}', media_type='audio/pcm')))
        return envelope

    def enqueue_turn(self, envelope) -> None:
        pending = []
        while not self.turn_queue.empty():
            previous, _ = self.turn_queue.get_nowait()
            pending.append(previous)
        self.turn_queue.put_nowait((self.merge_envelopes([*pending, envelope]), self.epoch))
        for _ in pending:
            self.turn_queue.task_done()

    async def _judge(self, text, speaker, overlap) -> InterruptDecision:
        snapshot = self.delivery.snapshot()
        if self.active_turn and (not snapshot or snapshot.get("state") in {"completed", "stopped", "failed"}):
            snapshot = self.active_turn
        snapshot = {"state": "completed", **snapshot, "attention": self.attention.hint(speaker, overlap=overlap),
                    "scene": [asdict(u) for u in list(self.scene)[-12:]]}
        # Do not accumulate model calls when one JEV timed out but is still running.
        if self.jev_future is not None and not self.jev_future.done():
            return VoiceJEV().decide(self.subject_id, text, asdict(speaker), snapshot, overlap=overlap)
        loop = asyncio.get_running_loop()
        self.jev_future = loop.run_in_executor(self.jev_executor, lambda:
            self.jev.decide(self.subject_id, text, asdict(speaker), snapshot, overlap=overlap))
        try:
            return await asyncio.wait_for(asyncio.shield(self.jev_future), self.timeout_s)
        except asyncio.TimeoutError:
            return VoiceJEV().decide(self.subject_id, text, asdict(speaker), snapshot, overlap=overlap)

    async def _work(self) -> None:
        while True:
            transcript = await self.queue.get()
            batch = [transcript]
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + max(10.0, self.input_pause_s * 2)
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
                binding_notes = []
                current = []
                from jshi.voice.jev import explicit_name
                for transcript in batch:
                    evidence = self.identities.resolve(transcript.track_id, voiceprint_id=transcript.voiceprint_id, confidence=transcript.confidence, cluster_id=transcript.speaker_cluster_id, uncertain=transcript.identity_uncertain)
                    claimed = explicit_name(transcript.text) if not transcript.overlap else ""
                    if claimed:
                        evidence, binding_note = self.identities.introduce(transcript.track_id, claimed, cluster_id=transcript.speaker_cluster_id, uncertain=transcript.identity_uncertain)
                        if evidence.status == "introduced" and self.local_speakers is not None:
                            learned = self.local_speakers.bind(transcript.track_id, evidence.object_id)
                            binding_note += (" 声纹特征已保存。" if learned else " 样本不足，声纹尚未保存。")
                        binding_notes.append(binding_note)
                        if evidence.status == "introduced":
                            self.attention.engage((evidence.object_id,))
                        await self.emit("identity", **asdict(evidence), detail=binding_note)
                    received = self.input_records.get(transcript.input_id, {}).get('received_at_ms')
                    origin = self.sample_source.started_at_ms if self.sample_source else None
                    utterance = SceneUtterance(transcript.text, evidence, transcript.start_ms, transcript.end_ms, transcript.overlap, transcript.input_id,
                        origin + transcript.start_ms if origin is not None else None,
                        origin + transcript.end_ms if origin is not None else None, received, transcript.identity_note)
                    self.scene.append(utterance)
                    current.append(utterance)
                    self.last_track = transcript.track_id
                    self.record("voice_scene_input", asdict(utterance))
                    await self.emit("timeline", **asdict(utterance))
                    await self.emit("speaker", **asdict(evidence))
                text = self.batch_text(current)
                evidence = current[-1].speaker
                active = self.delivery.snapshot().get("state") in {"playing", "paused", "queued"} or self.active_turn is not None
                explicit_stop = any(not u.overlap and VoiceJEV().decide(self.subject_id, u.text, {}, {}).action == 'stop' for u in current)
                busy = self.active_turn is not None and not explicit_stop
                decision = (InterruptDecision('stop', 'explicit stop') if explicit_stop else
                            InterruptDecision('respond', '主流程忙碌，累积到下一批输入') if busy else
                            await self._judge(current[0].text if len(current) == 1 else text, evidence, any(u.overlap for u in current)))
                self.pending_voice = False
                if busy:
                    await self.emit('input_pending', count=len(current))
                else:
                    self.last_jev = asdict(decision)
                    self.record("voice_jev", {"input": text, "track_id": transcript.track_id, **asdict(decision)})
                    await self.emit("jev", **asdict(decision))
                if decision.action in {"resume", "ignore"}:
                    await self.noise()
                    continue  # Scene/history already recorded; no main reply.
                elif (active and not busy) or decision.action == "stop":
                    await self.stop(decision.reason)
                if decision.action == "stop":
                    await self.emit("notice", text="已停止播放。")
                    continue
                # Delivery changes while cognition/write is busy. Resolve its
                # facts when the next turn actually begins, never at enqueue.
                note = ''
                import json
                note += "\n【接话关注】" + json.dumps([
                    {"object_id": u.speaker.object_id, **self.attention.hint(u.speaker, overlap=u.overlap)}
                    for u in current], ensure_ascii=False)
                note += "\n关注权重只供接话参考，不是声纹置信度。不降低新来者明确提问的资格；环境声的响度或尖锐程度不等于回应优先级。"
                if binding_notes:
                    note += "\n【身份关联】" + "\n".join(binding_notes)
                if self.unfinished(current[-1].text):
                    note += '\n【发言尚未完整】最后一句仍有未完句线索；可写场记录并等待续句，不抢答、不替对方补全含义。'
                if decision.action == "clarify":
                    note += "\n【JEV】本次发言不清楚，先请对方澄清，不猜测内容。"
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
                    overlap=any(u.overlap for u in current), delivery_context=note,
                    utterances=tuple(self.scene), current_utterances=tuple(current), deferred_interrupt=busy,
                )
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
            # Wait for the current speech group to settle before taking the
            # single accumulated next turn, including input received in write.
            await self.input_idle.wait()
            envelope, epoch = await self.turn_queue.get()
            if not self.input_idle.is_set():
                await self.input_idle.wait()
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
                if current:
                    speaker = current[-1].speaker if len({u.speaker.object_id for u in current}) == 1 else envelope.speaker
                    envelope = replace(envelope, current_utterances=current, utterances=tuple(annotated(u) for u in envelope.utterances),
                        speaker=speaker, parts=(InputPart('text', self.batch_text(current)), *envelope.parts[1:]))
                delivery = self.delivery.snapshot()
                previous = self.voice_timings.get(self.reply_epochs.get(delivery.get('reply_id')))
                order_note = ''
                if previous and previous[1].get('reply_ready_at_ms') and any(
                        u.received_at_ms is not None and u.received_at_ms <= previous[1]['reply_ready_at_ms'] for u in current):
                    order_note = '\n【发言与回应先后】本批有发言在当前回应生成之前到达，是上一轮过程中收到的补充。核对当前已生成、已播放和待播内容；已回答则 wait/think，只补未覆盖的信息，不复述同一答案。'
                envelope = replace(envelope, delivery_context=self.delivery.context_note() + '\n' + envelope.delivery_context + order_note)
                if self.last_debug and self.last_debug.get('timing'):
                    envelope = replace(envelope, delivery_context=envelope.delivery_context + '\n【上一轮实测耗时（毫秒）】' + json.dumps(self.last_debug['timing'], ensure_ascii=False, default=str) + '\n只据实测说明耗时；未测量的设备、网络或工具耗时不可断言。')
                if envelope.deferred_interrupt:
                    current = envelope.current_utterances
                    decision = await self._judge(current[0].text if len(current) == 1 else envelope.text,
                                                 envelope.speaker, envelope.overlap)
                    await self.emit('jev', **asdict(decision))
                    self.last_jev = asdict(decision)
                    self.record('voice_jev', {'input': envelope.text, **asdict(decision)})
                    if decision.action in {'ignore', 'resume'}:
                        await self.noise()
                        continue
                    if decision.action == 'stop' or (decision.action != 'resume' and self.delivery.snapshot().get('state') in {'playing', 'paused', 'queued'}):
                        await self.stop(decision.reason)
                        epoch = self.epoch
                    if decision.action == 'stop':
                        continue
                    envelope = replace(envelope, delivery_context=envelope.delivery_context + '\n【合批后接话判断】' + decision.reason)
                self.active_turn = {"state": "thinking", "object_id": envelope.speaker.object_id,
                                    "input": envelope.text, "pending_text": "尚未生成回应"}
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
        began = monotonic()
        rows = [self.input_records[u.input_id] for u in envelope.current_utterances if u.input_id in self.input_records]
        metrics = {'queue_wait_ms': round((began - min(r['at'] for r in rows)) * 1000) if rows else None,
                   'asr_lag_ms': max((r['asr_lag_ms'] for r in rows if r['asr_lag_ms'] is not None), default=None)}
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
            # Only enqueue on the event loop. Neither synthesis nor playback
            # blocks cognition or the independent write-zone call.
            valid = {u.speaker.object_id for u in (*envelope.utterances, *envelope.current_utterances)} | {envelope.speaker.object_id}
            items = tuple(ResponseItem(i.channel, i.text, tuple(t for t in i.target_ids if t in valid))
                          for i in plan.items if i.channel == "verbal" and i.text.strip()
                          and (not i.target_ids or any(t in valid for t in i.target_ids))) if plan.mode == "respond" else ()
            if not items:
                return
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._start_plan(items, envelope.speaker.object_id, epoch, turn_key)))

        await self.emit("thinking")
        def experience():
            result = self.process.experience(self.subject_id, envelope.text, envelope=envelope,
                objects={u.speaker.label:u.speaker.object_id for u in (*envelope.utterances, *envelope.current_utterances) if u.speaker.status in {"introduced", "recognized"}},
                resolved_speaker=self.identities.candidate(envelope.speaker), on_voice_plan=plan_ready)
            return result, self.process.last_model_response, result.selected_tool_ids

        result, response, selected = await loop.run_in_executor(self.turn_executor, experience)
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

    async def close(self) -> None:
        if self.closed:
            return
        await self.stop("connection closed")
        self.closed = True
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        self.input_records.clear()
        self.annotations.clear()
        self.worker.cancel()
        self.turn_worker.cancel()
        await asyncio.gather(self.worker, self.turn_worker, return_exceptions=True)
        if self.tts_task:
            await asyncio.gather(self.tts_task, return_exceptions=True)
        # Running synchronous cognition finishes its write; stale callbacks are
        # suppressed. Python cannot forcibly cancel a running HTTP request.
        await asyncio.to_thread(self.turn_executor.shutdown, wait=True, cancel_futures=True)
        await asyncio.to_thread(self.jev_executor.shutdown, wait=True, cancel_futures=True)


def create_app(process, subject_id: str, config: VoiceConfig, jev=None, local=None, local_tts=None, online_speakers=None, *, speaker_models=None, trial_root=None):
    from aiohttp import web, ClientSession, ClientTimeout, WSMsgType

    app = web.Application(client_max_size=128 * 1024)
    busy = False
    trial = None
    if speaker_models and trial_root:
        from jshi.voice.comparison import SpeakerTrial
        trial = SpeakerTrial(trial_root, speaker_models)

    async def page(request):
        return web.FileResponse(Path(__file__).with_name("voice_ui.html"))

    async def worklet(request):
        return web.FileResponse(Path(__file__).with_name("voice_capture.js"))

    async def status(request):
        return web.json_response({"busy": busy, "asr": "local" if local is not None else config.asr_backend,
            'speaker_models': list(speaker_models or {}),
            "local_available": local is not None, "online_available": bool(config.api_key)},
            headers={"Cache-Control": "no-store"})

    async def connection(request):
        nonlocal busy
        origin = request.headers.get("Origin", "")
        if origin != f"http://{request.host}":
            raise web.HTTPForbidden(text="voice connection requires same origin")
        if busy:
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
        ws = web.WebSocketResponse(max_msg_size=128 * 1024, heartbeat=20)
        await ws.prepare(request)
        conversation = None
        receiver = None
        asr = None
        local_executor = None
        cloud = None
        audio = SessionAudio()
        try:
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
                            use_local.diarizer.speakers = speakers
                if speakers is not None:
                    speakers.tracks.clear()
                    getattr(speakers, 'track_samples', {}).clear()
                    getattr(speakers, 'pending_tracks', []).clear()
                    speakers.last_embedding.clear()
                    speakers.binding.clear()
                conversation = VoiceConversation(process, subject_id, local_tts or cloud, ws.send_json, jev=jev,
                    timeout_s=config.jev_timeout_s, local_speakers=speakers, input_pause_s=config.input_pause_s, sample_source=audio)
                if use_local is not None:
                    use_local.reset_session()
                    local_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jshi-local-asr")
                else:
                    asr = await cloud.open_asr()
                    if speakers is not None:
                        local_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jshi-cloud-identity")
                assembler = TranscriptAssembler(identity_mode=config.identity_mode)

                async def receive():
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
                                    break
                            elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSED}:
                                raise RuntimeError("ASR connection closed")
                    except Exception as exc:
                        logging.getLogger(__name__).exception("Voice ASR receive failed")
                        await conversation.emit("notice", text=f"语音识别失败：{exc}")
                    finally:
                        if not ws.closed:
                            await ws.close()

                if asr is not None:
                    receiver = asyncio.create_task(receive())
                await conversation.emit("ready", identity_mode="local" if use_local else config.identity_mode,
                    voiceprint_available=speakers is not None,
                    voiceprint_count=len(getattr(speakers, 'known', {})) if speakers else 0,
                    match_threshold=getattr(speakers, 'threshold', None),
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
                                await conversation.emit('voice_settings', match_threshold=threshold, match_margin=speakers.margin)
                            except Exception as exc:
                                await conversation.emit('voice_settings_error', text=str(exc), match_threshold=getattr(speakers, 'threshold', None))
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
            if receiver:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            if asr:
                await asr.close()
            if conversation:
                await conversation.close()
            if cloud is not None and hasattr(cloud, 'close'):
                await cloud.close()
            if local_executor:
                await asyncio.to_thread(local_executor.shutdown, wait=True, cancel_futures=True)
            await ws.close()
            busy = False
        return ws

    app.router.add_get("/", page)
    app.router.add_get("/voice_capture.js", worklet)
    app.router.add_get("/status", status)
    app.router.add_get("/voice", connection)
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
