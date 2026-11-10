"""Local microphone UI; speech credentials never leave the Python server."""
from __future__ import annotations

import asyncio
import hashlib
import base64
import json
import logging
import os
import re
from time import monotonic, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4
from collections import deque

from jshi.core.envelope import InputEnvelope, InputPart, SceneUtterance, SpeakerEvidence
from jshi.core.params import VOICE_INPUT_PAUSE_MS, VISUAL_TURN_IDLE_SECONDS
from jshi.core.inputpack import pack_input, packet
from jshi.models import ResponseItem, ResponsePlan
from jshi.recognition import ObjectProfile, new_object_id
from jshi.subject.domain import HistoryKind, HistoryRecord
from jshi.voice.config import VoiceConfig
from jshi.voice.backends import ASR_BACKENDS, describe_asr
from jshi.voice.attention import ConversationAttention
from jshi.voice.audio_buffer import SessionAudio
from jshi.voice.delivery import DeliveryTracker
from jshi.voice.identity import VoiceIdentities, attach_voiceprint
from jshi.voice.interaction import InteractionFeedback
from jshi.voice.jev import BatchDecision, InterruptDecision, VoiceJEV
from jshi.subject.cancellation import TurnCancelled
from jshi.voice.timing import input_stage, stage, report_safely


async def bounded_online_match(audio, transcript, speakers, executor, pending, timeout_s):
    """Cloud recognition must not queue indefinitely behind local identity work."""
    if not transcript.final:
        return transcript, pending
    if pending is not None and not pending.done():
        return replace(transcript, identity_uncertain=True, identity_note='声纹后台仍在处理，本句未取得比对结果'), pending
    pending = asyncio.get_running_loop().run_in_executor(executor, audio.match, transcript, speakers)
    pending.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
    try:
        return await asyncio.wait_for(asyncio.shield(pending), timeout_s), pending
    except asyncio.TimeoutError:
        return replace(transcript, identity_uncertain=True, identity_note='声纹比对未及时返回，不延迟语音输入'), pending
    except Exception:
        logging.getLogger(__name__).exception('Online voice identity failed; transcript retained')
        return replace(transcript, identity_uncertain=True, identity_note='声纹比对失败，本句声音归属待定'), pending
from jshi.voice.contracts import Transcript
from jshi.voice.volc import VolcVoice


class VoiceConversation:
    def __init__(self, process, subject_id: str, cloud, send, *, jev=None, timeout_s=3.0, local_speakers=None, input_pause_s=VOICE_INPUT_PAUSE_MS / 1000, sample_source=None, candidate_timeout_s=1.2, session_id=None, input_max_batch_s=12.0, input_batch_chars=20, scene_idle_s=15.0, scene_batch_inputs=6, scene_batch_chars=800, scene_max_wait_s=30.0, environment_idle_s=VISUAL_TURN_IDLE_SECONDS, personal_speakers=None, audio_preparer=None, utterance_material=False) -> None:
        self.process, self.subject_id, self.cloud, self.send = process, subject_id, cloud, send
        self.utterance_material = utterance_material
        self.personal_speakers = personal_speakers
        from jshi.voice.sources import SpeakerAudioPreparer
        self.audio_preparer = audio_preparer or SpeakerAudioPreparer()
        self.session_id = session_id or uuid4().hex
        if not isinstance(self.session_id, str) or not self.session_id.isalnum() or len(self.session_id) > 64:
            raise ValueError("invalid voice session identifier")
        self.identities = VoiceIdentities(process.profiles, subject_id, self.session_id, process.repository)
        self.delivery = DeliveryTracker()
        from jshi.voice.dialogue import dialogue_state
        self.dialogue = dialogue_state(process)
        self.dialogue.import_history(subject_id, process.repository.list_history(subject_id, HistoryKind.FACT, 2000))
        previous = [r for r in process.repository.list_history(subject_id, HistoryKind.FACT, 100)
                    if r.event_type == "voice_delivery" and r.content.get("reply_id")]
        if previous:
            self.delivery.previous = dict(previous[-1].content)
        self.prior_session_spoken = (self.delivery.previous or {}).get("played_text") or ""
        self.jev = jev or VoiceJEV()
        from jshi.decision import decision_service
        from jshi.voice.entry_scheduler import EntryScheduler
        self.decisions = decision_service(process)
        self.entry_scheduler = EntryScheduler(self.decisions, self.record, self.stop)
        self.entry_scheduler.on_ready = self._release_deferred_entry
        self.entry_scheduler.work_busy = lambda: self.active_turn is not None or self.turn_busy or self.delivery.snapshot().get('state') in {'playing', 'paused', 'queued'}
        self.entry_scheduler.work_context = lambda: tuple(self.scene)[-12:]
        self.entry_scheduler.on_effect = self.apply_schedule_effect
        self.premise_changed = False
        from jshi.voice.provisional import PreparationManager
        from jshi.voice.feedback import FeedbackArbiter
        self.preparation = PreparationManager(self)
        self.feedback = FeedbackArbiter()
        self.speech_active = lambda: False
        self.local_speakers = local_speakers
        self.sample_source = sample_source
        from jshi.voice.utterance_organizer import UtteranceOrganizer
        self.utterance_organizer = UtteranceOrganizer()
        self.input_records = {}
        self.asr_timings = {}
        self.jev_pending_inputs = {}
        self.sample_bytes = 0
        self.annotations = {}
        self.personal_label_lock = asyncio.Lock()
        self.background_tasks = set()
        self.personal_prediction_lock = asyncio.Lock()
        self.personal_prediction_jobs = {}
        self.parallel_identity = None
        self.debug_history = deque(maxlen=12)
        self.voice_timings = {}
        self.last_jev = None
        from jshi.voice.direct_reply import reply_state
        self.direct_replies = reply_state(process)
        from jshi.voice.wait_ack import wait_timing
        self.wait_ack_timing = wait_timing(process)
        self.timeout_s = timeout_s
        # Legacy constructor settings remain accepted, but no longer govern collection.
        self.input_max_batch_s = max(.01, float(input_max_batch_s))
        self.input_batch_chars = max(0, int(input_batch_chars))
        self.input_carry = None
        self.accepting_batch = asyncio.Lock()
        self._last_jev_notice = 0.0
        self.input_pause_s = input_pause_s
        self.last_input_at = 0.0
        self.environment_idle_s = max(0, environment_idle_s)
        self.pending_environment = None
        self.environment_task = None
        self.turn_busy = False
        self.speech_input_at = {}
        self.input_idle = asyncio.Event()
        self.input_idle.set()
        self.queue = asyncio.Queue(maxsize=64)
        # Displayed speech groups awaiting entry judgment, in arrival order.
        self.judge_pending = deque()
        self.judge_ready = asyncio.Event()
        self.judging = False
        self.turn_queue = asyncio.Queue(maxsize=64)
        self.epoch = 0
        self.work_epoch_inputs = {}
        self.closed = False
        self.output_disconnected = False
        self.tts_task = None
        self.pending_voice = False
        self.last_track = ""
        self.reply_records = {}
        self.direct_reply_ids = set()
        self.recorded_inputs = deque(maxlen=256)
        self.reply_epochs = {}
        self.tool_handling = {}
        self.turn_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jshi-voice-turn")
        # A started HTTP call cannot be cancelled: one leftover call and the work-collision
        # entry may still be finishing while a fresh batch starts.
        self.jev_workers = 3
        self.jev_executor = ThreadPoolExecutor(max_workers=self.jev_workers, thread_name_prefix="jshi-voice-jev")
        self.jev_running = set()
        self.entry_scheduler.running_calls = lambda: tuple(f for f in self.jev_running if not f.done())
        self.permit_sequence = 0
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
        self.retired_turns = {}
        self.answer_status = {}
        self.answer_handling = deque(maxlen=3)
        self.answer_sound_codes = {}
        from jshi.voice.jev_scene import JEVSceneStore, JEVSceneWriter
        self.jev_scene_store = JEVSceneStore(process.repository.path.parent / "jev_scenes" / hashlib.sha256(subject_id.encode()).hexdigest()[:16] / (self.session_id + ".json"))
        from jshi.recognition.link_updates import retry_links
        retry_links(process,subject_id,personal=self.personal_speakers,live_scene=self.jev_scene_store,local=self.local_speakers)

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
        self.session_started_at_ms = time() * 1000
        self.last_settled_at_ms = self.session_started_at_ms
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
        self.judge_worker = asyncio.create_task(self._judge_work())
        self.turn_worker = asyncio.create_task(self._turns())
        executor = getattr(process, 'reflection_executor', None)
        self._reflection_token = executor.start(subject_id, lambda: (
            self.closed or self.pending_voice or self.active_turn is not None
            or not self.turn_queue.empty() or not self.queue.empty()
            or self.judging or bool(self.judge_pending)
            or self.delivery.snapshot().get('state') in {'playing', 'paused', 'queued'}
        )) if executor else None

    async def emit(self, kind: str, **payload) -> None:
        if kind in {'pause', 'resume', 'stop', 'block_next', 'clear_block'}:
            self.permit_sequence += 1
            payload['permit_seq'] = self.permit_sequence
        if kind == 'flow_phase':
            payload.setdefault('at_ms', time() * 1000)
        if not self.closed and not self.output_disconnected:
            try:
                await self.send({"type": kind, **payload})
            except ConnectionError:
                # The close handler still owns task/executor cleanup. Suppress
                # further browser writes during that close-handler race.
                self.output_disconnected = True

    def stamp_pipeline(self, iid, **times):
        row = self.input_records.get(iid)
        if row is None:
            return {}
        clock = row.setdefault('pipeline', {})
        for key, value in times.items():
            if value is not None:
                clock[key] = value
        return clock

    async def publish_pipeline(self, iids):
        for iid in iids:
            row = self.input_records.get(iid)
            if row and row.get('pipeline'):
                await self.emit('pipeline_clock', input_id=iid, pipeline=dict(row['pipeline']))

    def resolve_speaker(self, transcript):
        return self.identities.resolve(transcript.track_id, voiceprint_id=transcript.voiceprint_id,
            confidence=transcript.confidence, cluster_id=transcript.speaker_cluster_id,
            uncertain=transcript.identity_uncertain, tentative=transcript.identity_tentative,
            local_confirmation_threshold=getattr(self.local_speakers, 'confirmation_threshold', None))

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
                    if purpose != 'all' and r.get('purpose') != purpose and not (purpose == 'voice_jev' and r.get('purpose') in {'voice_jev_entry', 'voice_jev_identity', 'voice_jev_prepare'}):
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

    def link_selected_inputs(self, input_ids, target_id, *, basis='manual_annotation'):
        """Commit scoped identity evidence and project it to live conversation stores."""
        from jshi.recognition.acquaintance import acquaintance_store
        store = acquaintance_store(self.process)
        target = self.process.profiles.get(target_id)
        if target is None or target.status == 'rejected':
            raise ValueError('关联目标人物不存在')
        groups = {}
        for iid in input_ids:
            original = self.source_speakers.get(iid)
            if original and original.object_id != target_id:
                groups.setdefault(original.object_id, []).append(iid)
        from jshi.recognition.link_updates import MODULES, apply_link
        self.assign_code(target_id,target.label)
        keys=[]
        for source, ids in groups.items():
            payload={'session':self.session_id,'codes':dict(self.code_owner),'labels':dict(self.code_labels),'source_label':self.source_speakers[ids[0]].label,
                'texts':{i:self.input_records[i]['transcript'].text for i in ids if self.input_records.get(i,{}).get('transcript')},
                'bindings':{i:self.input_records.get(i,{}).get('personal_label_before',{}).get('binding') for i in ids},
                'sample_baselines':{i:self.input_records[i]['personal_label_before'].get('sample') for i in ids
                    if 'sample' in self.input_records.get(i,{}).get('personal_label_before',{})}}
            if self.local_speakers is not None and hasattr(self.local_speakers,'path'):
                payload['voice_bank']=os.path.relpath(self.local_speakers.path,self.process.repository.path.parent)
                payload['voice_model']=self.local_speakers.model_id
            from hashlib import sha256
            key=sha256(json.dumps([self.subject_id,self.session_id,source,target_id,sorted(ids)],ensure_ascii=False).encode()).hexdigest()
            key=store.link(self.subject_id,source,target_id,ids,basis=basis,modules=MODULES,link_id=key)
            store.facts(key,payload)
            apply_link(self.process,key,personal=self.personal_speakers,live_scene=self.jev_scene_store,local=self.local_speakers)
            for iid in ids:
                original=self.source_speakers[iid]
                self.annotations[iid]=replace(original,object_id=target_id,label=target.label,
                    status='recognized',method='manual_annotation' if basis.startswith('manual') else 'voiceprint_match')
            self.scene=deque((replace(u,speaker=self.annotations.get(u.input_id,u.speaker)) for u in self.scene),maxlen=24)
            keys.append(key)
        return keys

    def revoke_person_link(self, link_id):
        from jshi.recognition.acquaintance import acquaintance_store
        from jshi.recognition.link_updates import apply_link
        store=acquaintance_store(self.process)
        if link_id not in {r['id'] for r in store.links(self.subject_id,active_only=False)}:
            raise ValueError('人物关联不属于当前主体')
        link=next(r for r in store.links(self.subject_id,active_only=False) if r['id']==link_id)
        store.revoke(link_id)
        for iid in link['inputs']:
            original=self.source_speakers.get(iid)
            current=self.annotations.get(iid)
            if original and current and current.object_id==link['target']:
                self.annotations[iid]=original
                transcript=self.input_records.get(iid,{}).get('transcript')
                if transcript is not None:
                    for table,key in ((self.identities.tracks,transcript.track_id),
                                      (self.identities.clusters,transcript.speaker_cluster_id)):
                        existing=table.get(key)
                        if existing and existing.object_id==link['target']: table[key]=original
                    associated=self.identities.associations.get(original.object_id)
                    if associated and associated.object_id==link['target']:
                        self.identities.associations.pop(original.object_id,None)
        self.scene=deque((replace(u,speaker=self.annotations.get(u.input_id,u.speaker)) for u in self.scene),maxlen=24)
        return apply_link(self.process,link_id,personal=self.personal_speakers,live_scene=self.jev_scene_store,local=self.local_speakers)

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
                    old = self.resolve_speaker(t)
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
                    # A selection confirms these inputs only; keep other anonymous history isolated.
                    pass
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
            self.jev_scene_store.correct(self._scene_correction(input_ids, self.assign_code(evidence.object_id, evidence.label)),
                codes=self.code_owner, labels=self.code_labels)
            self.unknown_inputs.bind(tuple(input_ids), evidence.object_id)
            self.link_selected_inputs(input_ids,evidence.object_id,basis='manual_selection')
            if self.personal_speakers is not None:
                for iid, row in selected_pairs:
                    self.personal_speakers.submit_capture(row['pcm'], evidence.object_id, self.session_id,
                        iid, self.local_speakers, source='manual')
            await self.emit('enrollment', input_ids=input_ids, object_id=evidence.object_id, label=evidence.label, **stats)
        except Exception as exc:
            ids = list(dict.fromkeys(input_ids)) if isinstance(input_ids, list) else []
            problem_ids = [ids[i] for i in getattr(exc, 'sample_indices', ()) if i < len(ids)]
            await self.emit('enrollment_error', text=str(exc), problem_input_ids=problem_ids)

    async def label_personal_inputs(self, input_ids, object_id):
        """Human labels survive short-clip voiceprint enrollment rejection."""
        async with self.personal_label_lock:
            await self._label_personal_inputs(input_ids, object_id)

    async def _publish_personal_auto(self, input_id, job):
        try:
            sid = await asyncio.wrap_future(job['future'])
            if sid is None:
                return
            async with self.personal_label_lock:
                row = self.input_records.get(input_id)
                saved = await asyncio.to_thread(self.personal_speakers.store.sample, sid)
                if (not row or not saved or saved['source'] != 'voiceprint'
                        or 'personal_label_before' in row):
                    return
                row['personal_auto_saved'] = saved['object_id']
                person = self.process.profiles.get(saved['object_id'])
                if person is None:
                    return
                await self.emit('personal_auto_labelled', input_ids=[input_id],
                    object_id=saved['object_id'], label=person.label, score=saved['score'])
        except Exception as exc:
            self.record('personal_voice_auto_failed', {'input_id': input_id, 'reason': str(exc)})

    async def _label_personal_inputs(self, input_ids, object_id):
        try:
            if self.personal_speakers is None:
                raise ValueError('个人声音学习尚未启用')
            if not isinstance(input_ids, list) or not 1 <= len(input_ids) <= 32:
                raise ValueError('请选择 1–32 条声音')
            from jshi.voice.personal_store import OTHER_VOICE
            negative_only = object_id == OTHER_VOICE
            profile = None if negative_only else self.personal_speakers._person(object_id)
            selected = []
            for iid in dict.fromkeys(input_ids):
                row = self.input_records.get(str(iid))
                if not row or not row.get('pcm') or row['transcript'].overlap:
                    raise ValueError('所选声音已过期、没有音频或包含重叠声音')
                selected.append((str(iid), row))
            saved = []
            for iid, row in selected:
                before = row.get('personal_label_before')
                if before is None:
                    prior = self.annotations.get(iid) or next((u.speaker for u in self.scene if u.input_id == iid),
                        self.source_speakers.get(iid, SpeakerEvidence(row['transcript'].track_id)))
                    unknown = self.unknown_inputs.read(iid)
                    prior_sample=self.personal_speakers.store.sample(self.personal_speakers.store.sample_id(self.session_id,iid))
                    before = {'speaker': prior, 'annotation': self.annotations.get(iid),
                              'binding': unknown.get('manual_object_id') if unknown else None, 'sample':prior_sample}
                future = self.personal_speakers.submit_capture(row['pcm'], object_id, self.session_id,
                    iid, self.local_speakers, source='manual')
                if future is None:
                    raise ValueError('个人声音采集队列已满，请稍后再试')
                saved.append(await asyncio.wrap_future(future))
                row['personal_label_before'] = before
                if negative_only:
                    if before['annotation'] is None:
                        self.annotations.pop(iid, None)
                    else:
                        self.annotations[iid] = before['annotation']
                    self.unknown_inputs.bind((iid,), before['binding'])
                    self.scene = deque((replace(u, speaker=before['speaker']) if u.input_id == iid else u
                        for u in self.scene), maxlen=24)
                else:
                    self.annotations[iid] = SpeakerEvidence(row['transcript'].track_id, object_id,
                        profile.label, 'recognized', 'manual_annotation')
                    self.unknown_inputs.bind((iid,), object_id)
            self.scene = deque((replace(u, speaker=self.annotations.get(u.input_id, u.speaker)) for u in self.scene), maxlen=24)
            if not negative_only:
                self.jev_scene_store.correct(self._scene_correction(input_ids, self.assign_code(object_id, profile.label)), codes=self.code_owner, labels=self.code_labels)
            if not negative_only:
                self.link_selected_inputs(input_ids,object_id)
            self.record('personal_voice_manual_label', {'input_ids': input_ids, 'object_id': object_id,
                                                       'sample_ids': saved})
            await self.emit('personal_labelled', input_ids=input_ids, object_id=object_id,
                            label='其他（电视／电子设备，仅反例）' if negative_only else profile.label,
                            negative_only=negative_only, samples=len(saved))
        except Exception as exc:
            await self.emit('personal_label_error', text=str(exc),
                            input_ids=input_ids if isinstance(input_ids, list) else [], object_id=object_id)

    async def unlabel_personal_inputs(self, input_ids):
        async with self.personal_label_lock:
            try:
                if self.personal_speakers is None:
                    raise ValueError('个人声音学习尚未启用')
                if not isinstance(input_ids, list) or not 1 <= len(input_ids) <= 32:
                    raise ValueError('请选择 1–32 条声音')
                selected = [(str(iid), self.input_records.get(str(iid))) for iid in dict.fromkeys(input_ids)]
                if any(not row or not ('personal_label_before' in row or 'personal_auto_saved' in row)
                       for _, row in selected):
                    raise ValueError('这条声音没有可撤销的训练收录，或已过期')
                restored = []
                manual = []
                for iid, row in selected:
                    await asyncio.wrap_future(self.personal_speakers.collector.submit(
                        self.personal_speakers.store.withdraw, self.session_id, iid))
                    before = row.pop('personal_label_before', None)
                    row.pop('personal_auto_saved', None)
                    if before is not None:
                        manual.append(iid)
                        if before['annotation'] is None:
                            self.annotations.pop(iid, None)
                        else:
                            self.annotations[iid] = before['annotation']
                        self.unknown_inputs.bind((iid,), before['binding'])
                        self.scene = deque((replace(u, speaker=before['speaker']) if u.input_id == iid else u
                                            for u in self.scene), maxlen=24)
                        speaker = before['speaker']
                    else:
                        speaker = self.annotations.get(iid) or next((u.speaker for u in self.scene if u.input_id == iid),
                            self.source_speakers.get(iid, SpeakerEvidence(row['transcript'].track_id)))
                    restored.append({'input_id': iid, 'speaker': asdict(speaker), 'identity_unchanged': before is None,
                        'identity_display':self.identity_display(iid,speaker)})
                if manual:
                    self.jev_scene_store.correct(self._scene_correction(manual, undo=True),
                        codes=self.code_owner, labels=self.code_labels)
                from jshi.recognition.acquaintance import acquaintance_store
                for link in acquaintance_store(self.process).links(self.subject_id):
                    if set(link['inputs']).issubset(input_ids):
                        self.revoke_person_link(link['id'])
                self.record('personal_voice_manual_undo', {'input_ids': input_ids})
                await self.emit('personal_unlabelled', input_ids=input_ids, restored=restored)
            except Exception as exc:
                await self.emit('personal_label_error', text=str(exc), action='undo',
                                input_ids=input_ids if isinstance(input_ids, list) else [])

    async def _run_personal_prediction(self, transcript, record):
        """Score every compatible model independently of voiceprint candidates."""
        try:
            async with self.personal_prediction_lock:
                if self.input_records.get(transcript.input_id) is not record:
                    return
                if self.parallel_identity is not None:
                    future = await asyncio.to_thread(self.parallel_identity.predict,
                        record['pcm'], transcript.start_ms, transcript.end_ms,
                        track=(transcript.speaker_cluster_id or transcript.track_id)
                            if not transcript.overlap and not transcript.identity_uncertain
                            and transcript.audio_kind != 'separated' else '')
                else:
                    future = self.personal_speakers.submit_predict(record['pcm'], self.local_speakers)
                if future is None:
                    previous = getattr(self.personal_speakers, 'predict_future', None)
                    if previous is not None and not previous.done():
                        try:
                            await asyncio.shield(asyncio.wrap_future(previous))
                        except Exception:
                            pass
                        future = self.personal_speakers.submit_predict(record['pcm'], self.local_speakers)
                if future is None:
                    record['personal_classifier'] = {'status':'busy'}
                else:
                    result = dict(await asyncio.shield(asyncio.wrap_future(future)))
                    if getattr(future, 'identity_audio_bounds', (transcript.start_ms, transcript.end_ms)) != (transcript.start_ms, transcript.end_ms):
                        result['identity_basis'] = '同一段连续清晰声音，沿用先前人物证据；不是对新增尾部重新比对'
                    features = result.pop('debug_features', None)
                    if transcript.overlap and transcript.audio_kind != 'separated':
                        result.update(matched='', overlap=True)
                    if self.input_records.get(transcript.input_id) is not record:
                        return
                    record['personal_classifier'] = result
                    await self._archive_update(transcript.input_id, classifier=result,
                        classifier_result=self.personal_prediction_view(record), features=features)
                    try:
                        self.record('personal_voice_prediction', {'input_id':transcript.input_id, **result})
                    except Exception:
                        logging.getLogger(__name__).exception('Personal prediction diagnostics could not be saved')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record['personal_classifier'] = {'status':'error', 'detail':str(exc)}
            logging.getLogger(__name__).exception('Personal speaker scoring failed')
        if self.input_records.get(transcript.input_id) is record:
            try:
                speaker = self.annotations.get(transcript.input_id) or next(
                    (u.speaker for u in reversed(self.scene) if u.input_id == transcript.input_id),
                    self.source_speakers.get(transcript.input_id))
                view = self.identity_display(transcript.input_id, speaker) if speaker is not None else None
                await self._archive_update(transcript.input_id,
                    classifier_result=self.personal_prediction_view(record), **({'identity_display':view} if view else {}))
                await self.emit('personal_prediction', input_id=transcript.input_id,
                    personal_prediction=self.personal_prediction_view(record),
                    identity_display=view,
                    identity_comparison=self.identity_comparison(transcript.input_id))
            except Exception:
                logging.getLogger(__name__).exception('Personal speaker result could not be published')

    @input_stage('personal_identity')
    async def personal_identity(self, transcript, evidence, *, wait=True):
        record = self.input_records.get(transcript.input_id, {})
        if self.personal_speakers is None:
            record['personal_classifier'] = {'status':'not_configured'}
            return self.annotations.get(transcript.input_id, evidence)
        if not record.get('pcm'):
            record['personal_classifier'] = {'status':'no_audio'}
            return self.annotations.get(transcript.input_id, evidence)
        job = self.personal_prediction_jobs.get(transcript.input_id)
        if job is None:
            record['personal_classifier'] = {'status':'queued' if self.personal_prediction_lock.locked() else 'pending'}
            job = asyncio.create_task(self._run_personal_prediction(transcript, record))
            self.personal_prediction_jobs[transcript.input_id] = job
        if wait:
            try:
                await asyncio.wait_for(asyncio.shield(job), self.candidate_timeout_s)
            except asyncio.TimeoutError:
                # The job continues and publishes its real result to this input.
                pass
        manual = self.annotations.get(transcript.input_id)
        if manual is not None and manual.method == 'manual_annotation':
            return manual
        if evidence.status in {'recognized', 'introduced'}:
            return evidence
        result = record.get('personal_classifier', {})
        if result.get('matched'):
            row = next(r for r in result['ranking'] if r['object_id'] == result['matched'])
            return SpeakerEvidence(transcript.track_id, row['object_id'], row['label'],
                                   'recognized', 'personal_classifier', row['score'])
        return evidence

    @staticmethod
    def personal_prediction_view(record):
        """Publish this utterance's actual outputs, without internal version metadata."""
        import math
        result = record.get('personal_classifier', {})
        ranking = []
        for row in result.get('ranking', ()):
            score = row.get('score')
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                continue
            ranking.append({key: row[key] for key in ('object_id', 'label', 'score', 'threshold', 'verified', 'stale_labels', 'repeated_short_audio') if key in row})
        view = {'status': result.get('status', 'not_called'), 'matched': result.get('matched', ''),
                'ranking': ranking, 'overlap': result.get('overlap', False)}
        if result.get('short_audio_hint'):
            view['short_audio_hint'] = result['short_audio_hint']
        if result.get('status') == 'error':
            view['detail'] = str(result.get('detail', ''))[:180]
        return view

    def current_speaker(self):
        return next((u.speaker for u in reversed(self.scene)
                     if u.speaker.object_id and u.speaker.method != 'unassigned_audio'), None)

    async def typed(self, text, object_id='', *, receipt=None) -> None:
        """Text from the voice page speaks as the current person, and can correct a name."""
        typed_started, typed_wall = monotonic(), time()*1000
        receipt = receipt or {}
        self.feedback.changed()
        self.preparation.cancel()
        text = str(text or '').strip()
        original_text = text
        from jshi.simplified import simplify_text
        simplified = simplify_text(text)
        if simplified != text:
            self.record('original_text_evidence', {'source': 'typed', 'text': text})
            text = simplified
        if not text or len(text) > 2000 or any(ord(c) < 32 and c not in '\n\t' for c in text):
            await self.emit('typed_error' if object_id else 'notice', text='请输入 1–2000 个字。')
            return
        if object_id:
            self.last_input_at = monotonic()
            profile = self.process.profiles.get(object_id)
            if profile is None or profile.status == 'rejected' or profile.source == 'voice_scene':
                await self.emit('typed_error', text='所选人物不存在，请刷新人物列表。')
                return
            at = receipt.get('received_at_ms', typed_wall)
            iid = uuid4().hex
            start = max((u.end_ms for u in self.scene), default=0)
            if self.sample_source is not None:
                with self.sample_source.lock:
                    start = max(start, (self.sample_source.offset+len(self.sample_source.data))//32)
            evidence = SpeakerEvidence('typed:'+object_id, object_id, profile.label, 'recognized', 'user_selected', 1.0)
            if profile.source == 'voice_anonymous':
                from jshi.voice.jev import explicit_name
                from jshi.voice.identity import typed_name_answer
                name = explicit_name(text) or typed_name_answer(self.process,self.subject_id,evidence,text)
                if name:
                    self.identities.tracks[evidence.track_id] = evidence
                    evidence, _ = self.identities.introduce(evidence.track_id,name)
                    # Text selection remains bound to this person, not to an acoustic identity.
                    evidence = replace(evidence,status='recognized',method='user_selected')
                    profile = self.process.profiles.get(object_id)

            code = self.assign_code(object_id, profile.label)
            utterance = SceneUtterance(text, evidence, start, start, input_id=iid, received_at_ms=at,
                                       identity_note='文字输入；人物由用户选择，不是声纹确认')
            self.scene.append(utterance)
            self.input_records[iid] = {'pcm': None, 'received_at_ms': at, 'at': monotonic()}
            self.record('typed_input_timing', {
                'input_id':iid, **receipt, 'typed_started_at_ms':typed_wall,
                'task_queue_ms':round((typed_started-receipt.get('received_mono',typed_started))*1000,1),
                'prepare_ms':round((monotonic()-typed_started)*1000,1),
                'input_ready_at_ms':time()*1000})
            self.stamp_pipeline(iid,
                previous_settled_at_ms=self.last_settled_at_ms,
                session_started_at_ms=self.session_started_at_ms,
                server_received_at_ms=at, result_ready_at_ms=time()*1000, text_sent_at_ms=time()*1000)
            while len(self.input_records) > 128:
                old = next(iter(self.input_records))
                self.sample_bytes -= len(self.input_records.pop(old).get('pcm') or b'')
            await self.emit('typed_input', text=text, original_text=original_text, label=evidence.label, input_id=iid,
                            pipeline=dict(self.input_records[iid].get('pipeline') or {}))
            if (getattr(self.jev, 'mode', '') == 'decision'
                    and self.decisions.config['mode'] == 'active' and self.decisions.config['collision']):
                self.input_idle.clear()
                self.judge_pending.append({'current':[utterance], 'track_id':evidence.track_id,
                    'expression_complete':True, 'collection_trigger':'typed', 'queued':0, 'queued_at':monotonic()})
                self.entry_scheduler.notify((utterance,))
                self.judge_ready.set()
                return
            envelope = InputEnvelope(session_id=self.session_id, source='web_text',
                parts=(InputPart('text', text),), speaker=evidence, input_id=iid,
                start_ms=start, end_ms=start,
                utterances=tuple(self.scene), current_utterances=(utterance,), deferred_interrupt=True)
            if self.active_turn is not None or self.delivery.snapshot().get('state') in {'playing', 'paused', 'queued'}:
                input_epoch = self.epoch
                decision, record, items = await self._judge((utterance,), evidence, False, envelope)
                if input_epoch != self.epoch and record.get("applied_epoch") != self.epoch:
                    return
                current = tuple(self._apply_attribution((utterance,), items))
                control = record.get('control')
                if not control and decision.action == 'stop':
                    control = 'stop'  # Semantic result from a legacy compatible model.
                if control in {'stop', 'redirect'} and not record.get('control_applied'):
                    await self.stop('semantic_' + control)
                self._capture_jev_inputs(current, items)
                await self._publish_attribution(current, items, record)
                self._update_jev_continuation(current, record)
                if (control == 'stop' and not record.get('silent_operation')) or (control == 'keep' and record.get('route') not in {'main', 'self_reply'}):
                    return
                if not record.get('judged'):
                    self._preserve_background(current)
                    return
                envelope = replace(envelope, current_utterances=current, deferred_interrupt=False, jev_calls=(record,))
            self.enqueue_turn(envelope)
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
        from jshi.voice.diagnostics import DIAGNOSTIC_EVENTS, diagnostic_log
        path = getattr(self.process.repository, 'path', None)
        if event_type in DIAGNOSTIC_EVENTS and path is not None:
            diagnostic_log(path.parent/'voice_diagnostics.jsonl').write({
                'at_ms': round(time()*1000), 'event_type': event_type, 'subject_id': self.subject_id,
                'session_id': self.session_id, **payload})
            return
        self.process.repository.add_history(HistoryRecord(
            subject_id=self.subject_id, kind=HistoryKind.FACT, event_type=event_type,
            content={"session_id": self.session_id, **payload}, source_ids=()))

    async def pause(self) -> None:
        self.pending_voice = True
        if self.delivery.pause():
            await self.emit("pause", reply_id=self.delivery.snapshot()["reply_id"])

    async def noise(self) -> None:
        if self.entry_scheduler.schedule.blocks_start():
            return
        self.pending_voice = False
        if self.delivery.resume():
            await self.emit("resume", reply_id=self.delivery.snapshot()["reply_id"])

    async def stop(self, reason="explicit stop") -> None:
        self._retire_answers('替换' if reason == 'semantic_redirect' else '取消', reason=reason)
        effect = self.entry_scheduler.schedule.user_stop() if hasattr(self, 'entry_scheduler') else None
        if reason not in {'semantic_redirect', 'semantic_keep'} and hasattr(self, 'preparation'):
            self.preparation.cancel()
        self.epoch += 1
        rid = self.delivery.stop(reason)
        if self.tts_task and not self.tts_task.done():
            self.tts_task.cancel()
        self.pending_plans.clear()
        await self.emit("stop", reply_id=rid, permit_seq=effect.seq if effect else 0, reason_id=effect.reason if effect else reason)
        if effect:
            await self._persist_schedule(effect)
        if reason not in {'explicit stop', 'connection closed', 'semantic_stop'}:
            # Replacing an old reply must not cancel the new reply's permit.
            self.entry_scheduler.schedule.open_matter()
        try:
            self.dialogue.stop(self.session_id)
        except OSError:
            logging.getLogger(__name__).exception('Playback stopped but shared dialogue could not be saved')
        if reason in {'explicit stop', 'connection closed'}:
            self.jev_pending_inputs.clear()
            while not self.queue.empty():
                discarded = self.queue.get_nowait()
                try:
                    self.dialogue.input(self.subject_id, discarded.input_id, discarded.text,
                                        '声音归属待定', time()*1000)
                except OSError:
                    logging.getLogger(__name__).exception('Stopped input could not be saved')
                self.queue.task_done()
            if self.input_carry is not None:
                self.input_carry = None
                self.queue.task_done()
            while self.judge_pending:
                group = self.judge_pending.popleft()
                for utterance in group['current']:
                    try:
                        self.dialogue.input(self.subject_id, utterance.input_id, utterance.text,
                                            self.display_speaker(utterance.speaker), time()*1000)
                    except OSError:
                        logging.getLogger(__name__).exception('Stopped input could not be saved')
                for _ in range(group['queued']):
                    self.queue.task_done()
            self._refresh_input_idle()
            self.pending_voice = False
            while not self.turn_queue.empty():
                self.turn_queue.get_nowait()
                self.turn_queue.task_done()
        self.record("voice_delivery", self.delivery.snapshot())

        snapshot = self.delivery.snapshot()
        from jshi.voice.dialogue import delivery_brief
        if rid and snapshot.get('state') == 'stopped':
            oid = snapshot.get('object_id')
            profile = self.process.profiles.get(oid) if oid else None
            target = profile.label if profile else self.assign_code(oid, '') if oid else ''
            self.jev_scene_store.append(f"stopped:{rid}", {"p": "匠石", "at": time()*1000,
                "basis": "播放中断", "text": "播放已中断；曾开始的句子未确认完整播出，未播放内容不算已说。"})
            self.process.note_scene_delivery(self.subject_id, f"stopped:{rid}",
                delivery_brief(
                    self.dialogue.clean(snapshot, subject=self.subject_id), target=target))

    async def accept(self, transcript, *, asr_received_mono=None, asr_received_at_ms=None, online_identity_ms=0) -> None:
        # Capture before any emit, queue backpressure or archive I/O.
        accepted_mono, accepted_wall = monotonic(), time() * 1000
        receipt = (self.sample_source.receipt_timing(transcript.start_ms, transcript.end_ms)
                   if transcript.final and self.sample_source and hasattr(self.sample_source, 'receipt_timing') else {})
        clock_drift = (self.sample_source.clock_drift_ms(now=accepted_mono)
                       if self.sample_source and hasattr(self.sample_source, 'clock_drift_ms') else None)
        self.feedback.changed()
        from jshi.simplified import simplify_text
        simplified = simplify_text(transcript.text)
        if simplified != transcript.text:
            self.record('original_text_evidence', {'source': 'asr', 'input_id': transcript.input_id,
                        'start_ms': transcript.start_ms, 'end_ms': transcript.end_ms, 'text': transcript.text})
            transcript = replace(transcript, text=simplified)
        if transcript.replaces or transcript.replaces_input_ids:
            await self._revise_transcript(transcript)
            return
        from jshi.voice.transcripts import equivalent_regrouping
        repeated = equivalent_regrouping(transcript, self.input_records)
        if repeated:
            self.record('voice_asr_regrouping_ignored', {'covered_input_ids': list(repeated),
                'transcript': asdict(transcript), 'reason': '同一连续音频的相同字词重新分段，不新增发言或改变人物归属'})
            return
        if not any(ch.isalnum() for ch in transcript.text):
            await self.emit('transcript', **asdict(transcript))
            return  # No text: do not start or extend the conversational timer.
        speech_key = self.speech_group_key(transcript)
        received = asr_received_mono if asr_received_mono is not None else accepted_mono
        received_wall = asr_received_at_ms if asr_received_at_ms is not None else accepted_wall
        previous = self.speech_input_at.get(speech_key, self.last_input_at)
        gap_before = max(0, received - previous) if previous else 0
        self.last_input_at = received
        self.speech_input_at.pop(speech_key, None)
        self.speech_input_at[speech_key] = self.last_input_at
        while len(self.speech_input_at) > 128:
            self.speech_input_at.pop(next(iter(self.speech_input_at)))
        await self.emit("transcript", **asdict(transcript))
        if not transcript.final:
            if not transcript.track_id.startswith("pending-"):
                evidence = self.resolve_speaker(transcript)
                await self.emit("speaker", **asdict(evidence), jev_status="pending")
            return
        # Bound queue growth without discarding an already accepted input.
        # Waiting here only needs a free display slot; do not drop finished speech.
        noticed_backpressure = False
        while self.queue.full():
            if not noticed_backpressure:
                noticed_backpressure = True
                await self.emit("notice", text="待处理的发言较多，正在赶上，请稍候。")
            await asyncio.sleep(0.05)
        iid = transcript.input_id or uuid4().hex
        if iid in self.input_records:
            await self._revise_transcript(replace(transcript, replaces_input_ids=(iid,)))
            return
        transcript = replace(transcript, input_id=iid)
        report_safely(self.record, 'voice_input_stage', {'stage': 'input_accepted',
            'input_ids': [iid], 'source_start_ms': transcript.start_ms,
            'source_end_ms': transcript.end_ms, 'result_ready_at_ms': accepted_wall,
            'asr_received_at_ms': received_wall})
        pcm = self.sample_source.segment(transcript.start_ms, transcript.end_ms) if self.sample_source else None
        if pcm and len(pcm) > 30 * 32000:
            pcm = None
        lag = None
        if self.sample_source:
            lag = max(0, (self.sample_source.offset + len(self.sample_source.data)) / 32 - transcript.end_ms)
        self.input_records[iid] = {'transcript': transcript, 'pcm': pcm, 'at': received,
            'online_identity_ms':online_identity_ms,
            'gap_before':gap_before, 'asr_lag_ms': lag,
            'received_at_ms':received_wall}
        timing = self.asr_timings.pop((transcript.start_ms, transcript.end_ms, transcript.track_id), {})
        timing.update({k: v for k, v in receipt.items() if v is not None})
        timing['_result_ready_mono'] = accepted_mono
        timing.setdefault('_asr_return_mono', received)
        self.input_records[iid]['asr_timing'] = timing
        self.stamp_pipeline(iid,
            timing_version=2,
            previous_settled_at_ms=self.last_settled_at_ms,
            session_started_at_ms=self.session_started_at_ms,
            asr_returned_at_ms=self.input_records[iid]['received_at_ms'],
            result_ready_at_ms=accepted_wall,
            audio_start_received_at_ms=receipt.get('audio_start_received_at_ms'),
            audio_end_received_at_ms=receipt.get('audio_end_received_at_ms'),
            speech_audio_duration_ms=transcript.end_ms-transcript.start_ms,
            audio_clock_drift_ms=clock_drift)
        # Disk logging must not yield before enqueue, or the next sentence starts a new batch.
        self.schedule_voice_latency(iid, 'asr_returned')
        self.sample_bytes += len(pcm or b'')
        while len(self.input_records) > 128 or self.sample_bytes > 16 * 1024 * 1024:
            old = next(iter(self.input_records))
            self.sample_bytes -= len(self.input_records.pop(old)['pcm'] or b'')
            job = self.personal_prediction_jobs.pop(old, None)
            if job is not None and not job.done():
                job.cancel()
            self.annotations.pop(old, None)
            self.source_speakers.pop(old, None)
            self.candidate_cache.pop(old, None)
        self.input_idle.clear()
        if self.sample_source and getattr(self.sample_source,'archive',None):
            try:
                with stage(self.record, 'input_audio_archive', input_ids=[iid]):
                    await asyncio.to_thread(self.sample_source.archive.save_input,iid,
                        self.sample_source.capture_id,asdict(transcript),pcm=pcm,
                        received_at_ms=self.input_records[iid]['received_at_ms'])
                self.input_records[iid]['audio_saved'] = not self.sample_source.archive_error
                self._background(self._archive_update(iid, pipeline=dict(self.input_records[iid].get('pipeline') or {})))
            except Exception:
                logging.getLogger(__name__).exception('Recent input audio indexing failed')
                await self.emit('notice',text='本句录音保存失败；当前对话继续。')
        with stage(self.record, 'input_queue_put', input_ids=[iid]):
            await self.queue.put(transcript)

    @input_stage('input_batch_delivery')
    async def accept_batch(self, transcripts, **arrival):
        """Record one already-returned ASR batch before the worker drains it."""
        from jshi.voice.utterance_organizer import coalesce_rows
        states = arrival.pop('turn_states', {})
        transcripts = coalesce_rows(tuple(transcripts))
        updates = self.utterance_organizer.observe(transcripts)
        for row in transcripts:
            uid = self.utterance_organizer.sources.get((row.start_ms,row.end_ms))
            record = self.utterance_organizer.records.get(uid)
            state = states.get((row.start_ms,row.end_ms))
            if record and state in {'complete','incomplete'}:
                record.state = 'complete' if state == 'complete' else 'paused'
        updates = [self.utterance_organizer.records[u['utterance_id']].view() for u in updates
                   if u['utterance_id'] in self.utterance_organizer.records]
        if updates:
            self.record('utterance_organization', {'updates': updates})
        size = self.queue.maxsize or max(1,len(transcripts))
        for offset in range(0,len(transcripts),size):
            chunk = transcripts[offset:offset+size]
            # Deferred turns may release more rows than a single ASR window.
            # Apply backpressure before taking the drain lock; never drop rows
            # or deadlock the worker by waiting for space while holding it.
            # Wait only until the display worker frees slots. queue.join() also waits
            # for entry judgment, which blocked continuous speech for tens of seconds.
            while self.queue.maxsize and self.queue.qsize()+len(chunk) > self.queue.maxsize:
                await asyncio.sleep(0.05)
            async with self.accepting_batch:
                for transcript in chunk:
                    await self.accept(transcript, **arrival)
        if updates:
            for update in updates:
                for part in update['parts']:
                    part['input_id'] = next((iid for iid, record in self.input_records.items()
                        if record.get('transcript') is not None and
                        (record['transcript'].start_ms,record['transcript'].end_ms) ==
                        (part['start_ms'],part['end_ms'])), '')
            await self.emit('utterance_updates', updates=updates)

    def note_asr_timings(self, transcripts, timings):
        for transcript, timing in zip(transcripts, timings):
            if transcript.final:
                self.asr_timings[(transcript.start_ms, transcript.end_ms, transcript.track_id)] = timing
        while len(self.asr_timings) > 128:
            self.asr_timings.pop(next(iter(self.asr_timings)))

    async def turn_analysis(self, previews, decisions, timings=()):
        from jshi.voice.latency import append_latency
        payload = {'stage':'turn_analysis','session_id':self.session_id,
            'at_ms':time()*1000,'decisions':decisions,
            'ranges':[[t.start_ms,t.end_ms] for t in previews],
            'preview_timings':[{'audio_to_preview_ms':round((monotonic()-t['_detected_mono'])*1000,1)
                if t.get('_detected_mono') is not None else None,
                'analysis_batch_ms':t.get('analysis_batch_ms'),
                'transcription_batch_ms':t.get('transcription_batch_ms')} for t in timings]}
        try:
            await asyncio.to_thread(append_latency, self.process.repository.path.parent/'voice_latency.jsonl', payload)
        except OSError:
            logging.getLogger(__name__).exception('Turn analysis log could not be saved')
        await self.emit('turn_analysis', text='\n'.join(t.text for t in previews), decisions=decisions)

    def _background(self, coro):
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    def _latency_row(self, iid, stage, **values):
        from jshi.voice.latency import public_timing
        row = self.input_records.get(iid)
        if row is None:
            return None
        timing = row.setdefault('asr_timing', {})
        timing.update(values)
        result = public_timing(timing)
        result.update(stage=stage, input_id=iid, session_id=self.session_id,
            subject_id=self.subject_id, at_ms=time()*1000)
        return result

    async def _write_voice_latency(self, result):
        from jshi.voice.latency import append_latency
        try:
            await asyncio.to_thread(append_latency, self.process.repository.path.parent/'voice_latency.jsonl', result)
        except OSError:
            logging.getLogger(__name__).exception('Voice latency log could not be saved')

    def schedule_voice_latency(self, iid, stage, **values):
        result = self._latency_row(iid, stage, **values)
        if result is not None:
            self._background(self._write_voice_latency(result))
        return result

    def _record_stage_latency(self, envelope, main_started):
        from jshi.voice.latency import elapsed_ms
        for u in envelope.current_utterances:
            r = self.input_records.get(u.input_id)
            if r is None or r.get('stage_latency_recorded'):
                continue
            r['stage_latency_recorded'] = True
            first = r.get('jev_first_started_at')
            finished = r.get('jev_finished_at')
            self.schedule_voice_latency(u.input_id, 'main_started',
                audio_end_to_text_ms=elapsed_ms(r['at'], r.get('asr_timing', {}).get('_audio_end_received_mono')),
                text_to_jev_start_ms=elapsed_ms(first, r['at']),
                jev_span_ms=elapsed_ms(finished, first),
                jev_rounds=r.get('jev_rounds', 0),
                jev_to_main_ms=elapsed_ms(main_started, finished),
                text_to_main_ms=elapsed_ms(main_started, r['at']),
                speech_ms=(r['transcript'].end_ms-r['transcript'].start_ms) if r.get('transcript') else None)

    async def save_voice_latency(self, iid, stage, **values):
        result = self._latency_row(iid, stage, **values)
        if result is None:
            return
        await self._write_voice_latency(result)
        return result

    async def transcript_displayed(self, iid, render_ms=None):
        from jshi.voice.latency import elapsed_ms
        row = self.input_records.get(iid)
        if not row or 'text_sent_mono' not in row or row.get('display_acknowledged'):
            return
        row['display_acknowledged'] = True
        timing = row.get('asr_timing', {})
        result = await self.save_voice_latency(iid, 'browser_display_ack',
            text_send_to_display_ack_upper_ms=elapsed_ms(monotonic(), row['text_sent_mono']),
            detected_to_display_ack_upper_ms=elapsed_ms(monotonic(), timing.get('_detected_mono')),
            audio_end_received_to_display_ack_upper_ms=elapsed_ms(monotonic(), timing.get('_audio_end_received_mono')),
            browser_render_ms=round(render_ms, 1) if isinstance(render_ms, (int, float)) and not isinstance(render_ms, bool) and 0 <= render_ms <= 60000 else None)
        self.stamp_pipeline(iid, display_ack_at_ms=time()*1000)
        await self._archive_update(iid, voice_latency=result, pipeline=row.get('pipeline'))
        await self.emit('voice_latency', input_id=iid, timing=result, pipeline=dict(row.get('pipeline') or {}))

    @input_stage('input_archive_update')
    async def _archive_update(self,iid,**values):
        if self.sample_source and getattr(self.sample_source,'archive',None):
            try:
                await asyncio.to_thread(self.sample_source.archive.update,iid,**values)
            except Exception:
                logging.getLogger(__name__).exception('Recent audio diagnostics update failed')

    async def _revise_transcript(self, transcript):
        rows = sorted(((iid, row) for iid, row in self.input_records.items()
            if row.get('transcript') is not None and
            row.get('audio_kind') != 'separated' and
            (iid in transcript.replaces_input_ids or
             (row['transcript'].start_ms, row['transcript'].end_ms) in transcript.replaces)),
            key=lambda item: item[1]['transcript'].start_ms)
        if not rows:
            return  # old audio outside the retained session window is not new input
        if all(isinstance(row.get('superseded_by'), list) for _, row in rows):
            # A mixed-stream correction cannot replace independent source texts.
            for iid, row in rows:
                row['transcript'] = replace(row['transcript'], text=transcript.text)
                await self._archive_update(iid, transcript=asdict(row['transcript']))
            self.record('voice_asr_source_parent_revision', {'input_ids':[iid for iid,_ in rows],
                'text':transcript.text})
            return
        ids = [iid for iid, _ in rows]
        old = [row['transcript'].text for _, row in rows]
        if (transcript.audio_kind == 'revision_context' or
                len({row['transcript'].speaker_cluster_id or row['transcript'].track_id for _, row in rows}) > 1):
            correction = '【跨声音转写修订】原多段声音收到整体修订：“' + transcript.text + '”；字词与各人物尚未重新对齐，不把修订全文归给某一人。'
            self.record('voice_asr_cross_speaker_revision', {'input_ids': ids, 'text': transcript.text,
                'start_ms': transcript.start_ms, 'end_ms': transcript.end_ms})
            self.process.note_scene_delivery(self.subject_id, 'asr-cross-revision:' + uuid4().hex, correction)
            self.jev_scene_store.append('asr-cross-revision:' + uuid4().hex,
                {'text': correction, 'at': time()*1000, 'basis': 'asr_revision'},
                codes=self.code_owner, labels=self.code_labels)
            await self.emit('transcript_revision_pending', input_ids=ids, text=transcript.text)
            return
        first_id, first = rows[0]
        from jshi.voice.transcripts import equivalent_input_revision
        cosmetic = equivalent_input_revision(''.join(old), transcript.text)
        if not cosmetic:
            first['revision_version'] = first.get('revision_version', 0) + 1
            previous = list(first.get('asr_previous', ()))
            old_text = ' / '.join(old)
            if old_text and old_text != transcript.text and old_text not in previous:
                previous.append(old_text)
            first['asr_previous'] = previous[-2:]
        try:
            preserve_sentence = json.loads(transcript.identity_note or '{}').get('preserve_utterance') is True
        except (ValueError, TypeError, AttributeError):
            preserve_sentence = False
        first['transcript'] = replace(first['transcript'], text=transcript.text,
            start_ms=transcript.start_ms, end_ms=transcript.end_ms, words=transcript.words,
            **({'identity_note':transcript.identity_note, 'identity_uncertain':True}
               if preserve_sentence else {}))
        if self.sample_source and hasattr(self.sample_source, 'receipt_timing'):
            receipt = self.sample_source.receipt_timing(transcript.start_ms, transcript.end_ms)
            first.setdefault('asr_timing', {}).update({k:v for k,v in receipt.items() if v is not None})
            self.stamp_pipeline(first_id, speech_audio_duration_ms=transcript.end_ms-transcript.start_ms,
                **{k:v for k,v in receipt.items() if k in {'audio_start_received_at_ms','audio_end_received_at_ms'} and v is not None})
        await self._archive_update(first_id,transcript=asdict(first['transcript']))
        # Keep the original audio/evidence; a text correction cannot rename a person.
        for _, row in rows[1:]:
            row['superseded_by'] = first_id
        self.dialogue.revise_input(self.subject_id, ids, transcript.text)
        correction = '【语音识别修订】此前转写“' + ' / '.join(old) + '”应为“' + transcript.text + '”；同一段声音的文字修正，不是新发言，不据此改变人物归属。'
        self.record('voice_asr_revision', {'input_ids': ids, 'previous': old,
            'text': transcript.text, 'start_ms': transcript.start_ms, 'end_ms': transcript.end_ms})
        states = {self.answer_status.get(key) for key, owned in self.reply_input_ids.items()
                  if set(ids).intersection(owned) and key not in self.retired_turns}
        if states.intersection({'running', 'ready', 'playing'}):
            action = '保留' if cosmetic else '替换未播部分'
            reason = ('称呼或格式修正，沿用原回应' if cosmetic else
                      '按修订文字重新判断；已播部分保留为已发生事实，不能撤回')
        elif 'completed' in states:
            action, reason = '已播完', '旧回答已发生，不撤回、不自动重播；按修订内容判断是否需要补充纠正'
        else:
            action, reason = '尚无待播回答', '更新原输入，不当作再次发言'
        self._note_answer_handling(action, ids, transcript.text, reason)
        correction += f'旧回答：{action}；{reason}。'
        if not cosmetic:
            # A later correction can turn a suppressed repeat into a distinct request.
            first.pop('answer_reused_from', None)
            retired = self._retire_answers('替换', ids, reason='asr_revision')
            live = self.delivery.snapshot().get('reply_id')
            if self.reply_epochs.get(live) in retired:
                if self.tts_task and not self.tts_task.done():
                    self.tts_task.cancel()
                self.delivery.stop('asr_revision')
                await self.emit('stop', reply_id=live, reason_id='asr_revision')
        if not cosmetic and any(row.get('processing_started') for _, row in rows):
            self.process.note_scene_delivery(self.subject_id, 'asr-revision:'+uuid4().hex, correction)
            self.jev_scene_store.append('asr-revision:'+uuid4().hex,
                {'text': correction, 'at': time()*1000, 'basis': 'asr_revision'},
                codes=self.code_owner, labels=self.code_labels)
        await self.emit('transcript_revision', input_ids=ids, text=transcript.text, previous=old)
        changed = tuple(self._latest_utterances([u for u in self.scene if u.input_id == first_id]))
        if changed:
            self.scene = deque((changed[0] if u.input_id == first_id else u
                for u in self.scene if u.input_id not in ids[1:]), maxlen=self.scene.maxlen)
            if not cosmetic and any(row.get('processing_started') for _, row in rows):
                self.entry_scheduler.notify(changed)
                self.judge_pending.append({'current':list(changed), 'track_id':changed[0].speaker.track_id,
                    'expression_complete':None, 'collection_trigger':'revision', 'queued':0, 'queued_at':monotonic()})
                for u in changed:
                    report_safely(self.record, 'voice_input_stage', {
                        'stage': 'jev_input_queued', 'input_ids': [u.input_id], 'reason': 'revision'})
                self.judge_ready.set()

    def _latest_utterances(self, current):
        result = []
        seen = set()
        for u in current:
            if u.input_id in seen:
                continue
            seen.add(u.input_id)
            row = self.input_records.get(u.input_id, {})
            if row.get('superseded_by') or row.get('answer_reused_from'):
                continue
            latest = row.get('transcript')
            result.append(replace(u, text=latest.text, start_ms=latest.start_ms, end_ms=latest.end_ms,
                recorded_start_at_ms=(u.recorded_start_at_ms+latest.start_ms-u.start_ms
                                      if u.recorded_start_at_ms is not None else None),
                recorded_end_at_ms=(u.recorded_end_at_ms+latest.end_ms-u.end_ms
                                    if u.recorded_end_at_ms is not None else None)) if latest is not None else u)
        return result

    def _note_answer_handling(self, action, input_ids, text, reason):
        sources = []
        for iid in input_ids:
            source = self.input_records.get(iid, {}).get('transcript')
            track = source.track_id if source is not None else ''
            if not track or track.startswith(('unidentified-', 'pending-', 'overlap-')):
                continue
            code = self.answer_sound_codes.setdefault(track, f'答源{len(self.answer_sound_codes)+1}')
            if code not in sources:
                sources.append(code)
        who = '、'.join(sources) or '来源未定'
        note = f'{who}原话“{text[:80]}”：{action}。{reason}。'
        self.answer_handling.append((monotonic(), note))
        self.record('voice_answer_handling', {'action': action, 'input_ids': list(input_ids),
                    'text': text, 'reason': reason})

    def _answer_handling_context(self):
        notes = [note for at, note in getattr(self, 'answer_handling', ()) if monotonic()-at <= 120]
        return ('【回应处理】按发生顺序，后项可更新前项；答源仅关联声音，不确认身份。已播以回执为准。\n'
                + '\n'.join(notes)) if notes else ''

    def _retire_answers(self, action, input_ids=None, *, reason):
        """Invalidate only matching unplayed work; an ASR edit is not a global stop."""
        retired = set()
        for key, ids in getattr(self, 'reply_input_ids', {}).items():
            if input_ids is not None and not set(input_ids).intersection(ids):
                continue
            if key in self.retired_turns or self.answer_status.get(key) in {'completed', 'silent', 'failed'}:
                continue
            self.retired_turns[key] = reason
            retired.add(key)
            if reason != 'asr_revision':
                text = ' / '.join(self.input_records[i]['transcript'].text for i in ids
                                if self.input_records.get(i, {}).get('transcript'))
                self._note_answer_handling(action, ids, text, '旧回应未播部分已作废')
        self.pending_plans = deque((p for p in self.pending_plans if p[3] not in retired), maxlen=32)
        while len(self.retired_turns) > 64:
            self.retired_turns.pop(next(iter(self.retired_turns)))
        return retired

    def _retain_repeated_requests(self, current):
        """Exact repeats from the same sound retain a still-pending response, never a played one."""
        from jshi.voice.transcripts import same_request_wording
        kept = []
        for u in current:
            found = None
            for key, ids in reversed(list(self.reply_input_ids.items())):
                if key in self.retired_turns or self.answer_status.get(key) not in {'running', 'ready', 'playing'}:
                    continue
                for iid in ids:
                    old = self.input_records.get(iid, {}).get('transcript')
                    # Acoustic continuity only; no deduplication across uncertain/overlapping voices.
                    if (old is not None and iid != u.input_id and not old.overlap and not u.overlap
                            and not old.identity_uncertain and old.track_id == u.speaker.track_id
                            and old.track_id and not old.track_id.startswith(('unidentified-', 'pending-', 'overlap-'))
                            and 0 <= u.start_ms - old.end_ms <= 30000
                            and same_request_wording(old.text, u.text)):
                        found = iid
                        break
                if found:
                    break
            if found:
                self.input_records[u.input_id]['answer_reused_from'] = found
                self._note_answer_handling('保留', (u.input_id, found), u.text, '同一声音重问，原回应仍在生成或待播，不另生成一份')
            else:
                kept.append(u)
        return kept

    async def request_emergency_interrupt(self, reason: str, *, expected_epoch=None) -> bool:
        """预留给明确紧急事件的入口；调用方负责判定，不做危险检测。"""
        if self.closed or (expected_epoch is not None and expected_epoch != self.epoch):
            return False
        self.record("voice_emergency_interrupt", {"reason": str(reason), "epoch": self.epoch})
        await self.stop("explicit stop")
        return True

    @staticmethod
    def speech_group_key(transcript):
        # Only supplied acoustic continuity is used; this does not assign a name.
        if not transcript.overlap and not transcript.identity_uncertain:
            if transcript.voiceprint_id:
                return ('voiceprint', transcript.voiceprint_id)
            if transcript.speaker_cluster_id:
                return ('cluster', transcript.speaker_cluster_id)
            if transcript.track_id and not transcript.track_id.startswith(('pending-', 'unidentified-', 'overlap-')):
                return ('track', transcript.track_id)
        # A provisional track only isolates its text-activity clock. It does
        # not establish same-person identity, a name, or trusted voice evidence.
        if not transcript.overlap and transcript.track_id and not transcript.track_id.startswith('overlap-'):
            return ('provisional', transcript.track_id)
        return None

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

    def reply_target_name(self, object_id: str) -> str:
        """Name shown after 匠石 →. Never the internal object id."""
        profile = self.process.profiles.get(object_id) if object_id else None
        label = getattr(profile, 'label', '') or ''
        source = getattr(profile, 'source', '')
        if not object_id or source == 'voice_scene' or label == '语音现场':
            return '现场'
        remembered = self.code_labels.get(object_id, '') if hasattr(self, 'code_labels') else ''
        for candidate in (label, remembered):
            if candidate and candidate != object_id and not str(candidate).startswith('OBJ-'):
                return candidate
        return '对方'

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
            lines.append('JEV 初判：人物归属见标注；主认知结合交往材料决定是否及如何回应。')
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
        lines.extend("（程序说明）" + fact for fact in facts if fact)
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

    def _scene_correction(self, input_ids, person='', *, undo=False):
        corrected = []
        for iid in input_ids:
            record = self.input_records.get(iid, {})
            utterance = next((u for u in self.scene if u.input_id == iid), None)
            original = self.source_speakers.get(iid)
            transcript = record.get('transcript')
            if transcript is None:
                continue
            speaker = self.annotations.get(iid) or (utterance.speaker if utterance else original)
            code = person or (self.assign_code(speaker.object_id, speaker.label)
                if speaker and speaker.status == 'recognized' else 'unknown')
            corrected.append({'ref': iid, 'text': transcript.text,
                'at': utterance.received_at_ms if utterance else record.get('asr_returned_at_ms'),
                'old_person': self.assign_code(original.object_id, original.label)
                    if original and original.object_id and original.method != 'unassigned_audio' else 'unknown',
                'person': code})
        return {'basis': 'manual_annotation_undo' if undo else 'manual_annotation',
            'p': person or 'unknown', 'scope': 'selected_events_only',
            'text': '用户撤销所列发言的人工身份依据' if undo else '用户仅确认所列发言属于' + person,
            'corrected_events': corrected, 'at': time()*1000}

    def _preserve_background(self, utterances):
        for u in utterances:
            self.process.note_entry_background(self.subject_id, u)

    def _capture_jev_inputs(self, current, items=(), *, entry_status=None, interaction=None):
        marks = {r.n: r for r in items}
        evidence_rows = self._jev_batch(current)
        for n, u in enumerate(current, 1):
            latest = self.input_records.get(u.input_id, {}).get('transcript')
            if latest is not None:
                u = replace(u, text=latest.text)
            if self.input_records.get(u.input_id, {}).get('superseded_by'):
                continue
            self.dialogue.input(self.subject_id, u.input_id, u.text, u.speaker.label, u.received_at_ms, actor_id=u.speaker.object_id)
            if u.input_id not in self.recorded_inputs:
                if u.speaker.method == 'user_selected':
                    self.record('web_text_input', {'input_id':u.input_id, 'text':u.text,
                        'speaker':asdict(u.speaker), 'at':u.received_at_ms})
                else:
                    self.record('voice_scene_input', asdict(u))
                self.recorded_inputs.append(u.input_id)
            code = self.assign_code(u.speaker.object_id, u.speaker.label) if u.speaker.method not in {"unassigned_audio", "visual_scene"} else "unknown"
            row = {"p": code, "text": u.text, "at": u.received_at_ms, "basis": u.speaker.method,
                   "status": u.speaker.status, "track": u.speaker.track_id, "input_id": u.input_id,
                   "entry_status": entry_status or ('completed' if n in marks and marks[n].speaker_pick else 'failed'),
                   "voice": evidence_rows[n-1]['voice_evidence'],
                   "joint_evidence": evidence_rows[n-1].get('joint_evidence',{}),
                   "candidates": [{"p": c['who'],
                                   ('classification_score' if c.get('source')=='personal_classifier' else 'similarity'):c.get('score'),
                                   "source": c.get('source'), "verified":c.get('verified'),
                                   "names": c.get('names', [])} for c in evidence_rows[n-1]['candidates']],
                   "short_voice_hint": evidence_rows[n-1]['short_voice_hint']}
            source_record = self.input_records.get(u.input_id, {})
            if interaction in {'direct', 'uncertain', 'none'}:
                source_record['entry_interaction'] = interaction
            if source_record.get('entry_interaction') in {'direct', 'uncertain', 'none'}:
                # Recapturing the same input for main must not erase its prior direction.
                row['entry_interaction'] = source_record['entry_interaction']
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
                passes = 0
                while not self.closed:
                    self._sync_scene_updates()
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
                        model_snapshot = self.dialogue.clean(snapshot, subject=self.subject_id)
                        from jshi.core.person_names import known_names
                        model_snapshot['named_people'] = known_names(self.code_owner, self.process.profiles)
                        result = self.person_reviewer.run(self.subject_id, model_snapshot, self.dialogue.filter_text(main_scene, subject=self.subject_id),
                            on_request=lambda req: self.process._record_step_input(req, self.person_reviewer.model,
                                subject_id=self.subject_id, activity_id=review_id))
                        snapshot['material_window'] = model_snapshot.get('material_window', {})
                        snapshot['discard_history'] = model_snapshot.get('discard_history', [])
                        snapshot['ignored_output_fields'] = model_snapshot.get('ignored_output_fields', {})
                        return result
                    started = monotonic()
                    self._last_scene_attempt = started
                    scene, raw = await loop.run_in_executor(self.person_review_executor, run)
                    self._sync_scene_updates()
                    accepted = not self.closed and self.jev_scene_store.commit(snapshot, scene)
                    self.record("voice_jev_scene", {"review_id": review_id, "accepted": accepted,
                        "trigger": trigger,
                        "model_raw": raw, "scene": scene, "chars": len(scene),
                        "ignored_output_fields": snapshot.get('ignored_output_fields', {}),
                        "version": self.jev_scene_store.snapshot()["version"],
                        "covered_input_ids": snapshot.get("material_window", {}).get("model_input_ids", []) if accepted else [],
                        "material_window": snapshot.get("material_window", {}),
                        "elapsed_ms": round((monotonic()-started)*1000)})
                    failures = 0
                    passes += 1
                    if accepted and passes < 2 and snapshot.get('material_window', {}).get('deferred_input_ids'):
                        continue  # Read the next chronological prefix; never skip unread dialogue.
                    break  # new input is coalesced by the next main/fallback trigger
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
                                   "score": (round(candidate['score'], 3)
                                             if candidate.get('source') == 'personal_classifier' and isinstance(candidate.get('score'), (int, float))
                                             else candidate.get("score")), "source": candidate.get("source", "voiceprint"),
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

    @input_stage('voice_candidates')
    async def rank_candidates(self, transcript, speaker):
        record = self.input_records.get(transcript.input_id, {})
        if speaker.status in {"introduced", "recognized"}:
            try:
                matched = json.loads(transcript.identity_note or '{}')
            except ValueError:
                matched = {}
            stats = next((row for row in matched.get('candidates', ()) if row.get('object_id') == speaker.object_id), {})
            return [{**{key:stats[key] for key in ('reference_count', 'stddev', 'sample_limited') if key in stats},
                     "object_id": speaker.object_id, "label": speaker.label,
                     "score": speaker.confidence if speaker.method == 'personal_classifier' else transcript.confidence,
                     "source": speaker.method}], "already_identified"
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
        if short and ((transcript.overlap and transcript.audio_kind != 'separated') or len(pcm) < 6400):
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
                await self.emit('write_started')
                write_ids = list(self.input_records)[-8:]
                at = time()*1000
                for iid in write_ids:
                    self.stamp_pipeline(iid, write_started_at_ms=at)
                await self.publish_pipeline(write_ids)
                wrote = await asyncio.get_running_loop().run_in_executor(self.write_executor, job)
                if wrote is False:
                    self.failed_write_jobs.append(job)
                    await self.emit("notice", text="写场未完成，输入与回应已保留；主流程继续，下一批合并重写。")
                    await self.emit("write_failed")
                elif wrote is True:
                    self.failed_write_jobs.clear()
                    done = time()*1000
                    for iid in write_ids:
                        self.stamp_pipeline(iid, write_completed_at_ms=done)
                    await self.publish_pipeline(write_ids)
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
        if self.closed:
            return
        if envelope.source == 'visual_environment':
            self.defer_environment(envelope)
            return
        pending = []
        drained = 0
        while not self.turn_queue.empty():
            previous, _ = self.turn_queue.get_nowait()
            drained += 1
            if previous.source == 'visual_environment':
                self.defer_environment(previous)
            else:
                pending.append(previous)
        self.turn_queue.put_nowait((self.merge_envelopes([*pending, envelope]), self.epoch))
        queued_at_ms = time()*1000
        for u in envelope.current_utterances:
            self.stamp_pipeline(u.input_id, queued_at_ms=queued_at_ms)
        asyncio.create_task(self.emit('flow_phase', lane='input', stage='queued'))
        asyncio.create_task(self.publish_pipeline(u.input_id for u in envelope.current_utterances if u.input_id))
        for _ in range(drained):
            self.turn_queue.task_done()

    def defer_environment(self, envelope):
        self.pending_environment = envelope
        if self.environment_task is None or self.environment_task.done():
            self.environment_task = asyncio.create_task(self._environment_turns())

    async def _environment_turns(self):
        while self.pending_environment is not None and not self.closed:
            await asyncio.sleep(.05)
            if (self.turn_busy or self.active_turn or not self.input_idle.is_set()
                    or not self.queue.empty() or not self.turn_queue.empty()
                    or (self.jev_future is not None and not self.jev_future.done())
                    or self.delivery.snapshot().get('state') in {'playing', 'paused', 'queued'}
                    or monotonic()-self.last_input_at < self.environment_idle_s):
                continue
            envelope, self.pending_environment = self.pending_environment, None
            self.turn_queue.put_nowait((envelope, self.epoch))

    def voice_evidence(self, utterance, candidates):
        evidence = self._voice_evidence(utterance, candidates)
        if utterance.speaker.method == 'manual_annotation':
            return evidence
        try:
            note = json.loads(utterance.identity_note or '{}')
        except (ValueError, TypeError):
            note = {}
        personal = self.input_records.get(utterance.input_id, {}).get('personal_classifier', {})
        if (isinstance(note, dict) and note.get('identity_basis')) or personal.get('identity_basis'):
            evidence['basis'] += '；同一连续声音沿用前段人物证据，未重新比对新增尾部'
        return evidence

    def _voice_evidence(self, utterance, candidates):
        """Physical support uses original measurements, never a previous model guess."""
        import math
        if utterance.speaker.method == 'manual_annotation':
            return {'pick': self.assign_code(utterance.speaker.object_id, utterance.speaker.label),
                    'strength': 'clear', 'basis': '用户人工确认本句归属'}
        original = self.source_speakers.get(utterance.input_id, utterance.speaker)
        if original.method == "voiceprint_match":
            evidence = {"pick": self.assign_code(original.object_id, original.label), "strength": "clear", "basis": "声纹已匹配"}
            threshold = getattr(self.local_speakers, "confirmation_threshold", None)
            confidence = getattr(original, 'confidence', None)
            if type(confidence) in (int, float) and math.isfinite(confidence):
                evidence["score"] = confidence
                if type(threshold) in (int, float) and str(getattr(original, 'voiceprint_id', '') or '').startswith('local:'):
                    evidence["threshold"] = threshold
            return evidence
        personal = self.input_records.get(utterance.input_id, {}).get('personal_classifier', {})
        if personal.get('matched'):
            row = next((r for r in personal.get('ranking', ()) if r['object_id'] == personal['matched']), None)
            if row:
                return {'pick': self.assign_code(row['object_id'], row['label']), 'strength': 'clear',
                        'score': round(row['score'], 3), 'threshold': round(row['threshold'], 3),
                        'basis': '个人二分类模型通过独立验证后的门槛；分类分数不是原声纹相似度或身份概率'}
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

    def identity_comparison(self,input_id):
        from jshi.voice.identity_comparison import compare_identity
        comparison = compare_identity(self.candidate_cache.get(input_id,[]),
            self.input_records.get(input_id,{}).get('personal_classifier',{}),
            voice_threshold=getattr(self.local_speakers,'confirmation_threshold',getattr(self.local_speakers,'threshold',.33)),
            voice_margin=getattr(self.local_speakers,'margin',.08))
        if self.input_records.get(input_id,{}).get('personal_classifier',{}).get('overlap'):
            comparison.update(mlp_high=False, mlp_priority=False)
        winner = comparison.get('mlp',{}).get('object_id')
        if any(r.get('object_id') == winner and r.get('stale_labels')
               for r in self.input_records.get(input_id,{}).get('personal_classifier',{}).get('ranking',[])):
            comparison.update(mlp_high=False, mlp_priority=False)
        return comparison

    def identity_display(self,input_id,speaker,certainty=''):
        from jshi.voice.identity_comparison import identity_display
        return identity_display(asdict(speaker),self.identity_comparison(input_id),certainty=certainty,
            classifier=self.input_records.get(input_id,{}).get('personal_classifier',{}),
            original=asdict(self.source_speakers.get(input_id,speaker)))

    @input_stage('jev_people_material')
    def _jev_batch(self, utterances):
        utterances = tuple(utterances)
        batch = []
        ids = {r['object_id'] for u in utterances for r in (
            *self.candidate_cache.get(u.input_id, []),
            *self.input_records.get(u.input_id, {}).get('personal_classifier', {}).get('ranking', []))}
        profiles = self.process.profiles
        names = (profiles.name_material(ids) if hasattr(profiles, 'name_material') else
                 {oid:(profiles.get(oid),profiles.address_line(oid)) for oid in ids})
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
                profile, address = names.get(item['object_id'], (None, ''))
                candidates.append({"who": code, "score": item.get("score"), "source": item.get("source") or "voiceprint",
                    **{key:item[key] for key in ('reference_count', 'stddev', 'sample_limited') if key in item},
                    "names": [profile.label, *profile.aliases] if profile else [],
                    "portrait_names": address})
            personal=self.input_records.get(utterance.input_id,{}).get('personal_classifier',{})
            for item in personal.get('ranking',[]):
                code=self.assign_code(item['object_id'],item.get('label',''))
                existing=next((c for c in candidates if c['source']=='personal_classifier' and c['who']==code),None)
                if existing is not None:
                    existing.update(verified=item.get('verified',True),threshold=item.get('threshold',.8))
                    if item.get('repeated_short_audio'):
                        existing['applicability'] = '同一短句重复拼接的MLP参考，只支持可能人物；原声未变长，不是独立证据'
                    continue
                profile=names.get(item['object_id'], (None, ''))[0]
                if profile is None or profile.status=='rejected':continue
                candidates.append({'who':self.assign_code(item['object_id'],profile.label),'score':item['score'],
                    'source':'personal_classifier','verified':item.get('verified',True),
                    **({'applicability':'同一短句重复拼接的MLP参考，只支持可能人物；原声未变长，不是独立证据'}
                       if item.get('repeated_short_audio') else {}),
                    'threshold':item.get('threshold',.8),'names':[profile.label,*profile.aliases]})
            comparison=self.identity_comparison(utterance.input_id)
            joint={k:v for k,v in comparison.items() if k not in {'voice','mlp'}}
            for kind in ('voice','mlp'):
                if kind in comparison:
                    r=comparison[kind]
                    joint[kind]={'p':self.assign_code(r['object_id'],r.get('label','')),
                        **{k:round(v,3) if type(v) is float else v for k,v in r.items() if k not in {'object_id','label'}}}
            check = self.input_records.get(utterance.input_id, {})
            measured = [r for r in candidates if r.get('source') in {'voiceprint', 'voiceprint_match', 'repeated_short_audio'}
                        and type(r.get('score')) in (int, float)]
            voice = self.voice_evidence(utterance, candidates)
            voice['measurement'] = {
                'status': check.get('candidate_status', 'not_recorded'),
                'result': '已取得相似度；是否确认以本句voice结论为准' if measured else '本轮未取得可用相似度；检查status原因，不能等同未运行',
                'repeated_short_audio': bool(check.get('short_voice_hint'))}
            batch.append({"n": index, "input_id": utterance.input_id, "track": utterance.speaker.track_id,
                          "sound": self.sound_code(utterance),
                          "who": self.display_speaker(utterance.speaker), "text": utterance.text,
                          "seconds": round(max(0, utterance.end_ms - utterance.start_ms) / 1000, 2),
                          "basis": utterance.speaker.method, "at": utterance.received_at_ms, "at_ms": utterance.start_ms, "candidates": candidates,
                          "short_voice_hint": self.input_records.get(utterance.input_id, {}).get('short_voice_hint', {}),
                          "asr_previous": [s if len(s) <= 300 else s[:300]+'…（此前转写较长，此处截取）'
                                           for s in check.get('asr_previous', ())],
                          "joint_evidence":joint,"voice_evidence": voice})
        return batch

    def _jev_context(self, utterances):
        current_ids = {u.input_id for u in utterances}
        cutoff = min((u.received_at_ms for u in utterances if u.received_at_ms is not None), default=time()*1000)
        _, context = self.jev_scene_store.view(cutoff, current_ids)
        # Explicit JEV holds must survive background scene summarization. Preserve
        # original wording and attribution; do not concatenate different speakers.
        max_age_ms = float(os.getenv('JSHI_JEV_CONTEXT_IDLE_SECONDS', '300'))*1000
        self.jev_pending_inputs = {iid:u for iid,u in self.jev_pending_inputs.items()
            if u.received_at_ms is not None and cutoff-u.received_at_ms <= max_age_ms}
        present = current_ids | {r.get('input_id') for r in context}
        for iid, u in self.jev_pending_inputs.items():
            if iid not in present and u.received_at_ms <= cutoff:
                context.append({'input_id':iid, 'text':u.text, 'at':u.received_at_ms,
                    'p':self.assign_code(u.speaker.object_id,u.speaker.label)
                        if u.speaker.method != 'unassigned_audio' else 'unknown',
                    'track':u.speaker.track_id,'basis':u.speaker.method,
                    'entry_status':'pending_continuation'})
        context.sort(key=lambda r:r.get('at') or 0)
        from jshi.voice.jev_scene import recent_episode
        heard = recent_episode(list(self.played_history), cutoff)
        return context, "\n".join(r["text"] for r in heard)

    def _hold_jev_inputs(self, utterances):
        for u in utterances:
            self.jev_pending_inputs[u.input_id] = u
        while len(self.jev_pending_inputs) > 32 or sum(len(u.text) for u in self.jev_pending_inputs.values()) > 8000:
            self.jev_pending_inputs.pop(next(iter(self.jev_pending_inputs)))

    def _update_jev_continuation(self, utterances, record):
        """Completeness owns retention; control owns actions. Old responses stay compatible."""
        status = record.get('utterance_complete', 'uncertain')
        if record.get('entry_route'):
            pending = set(record.get('pending_refs', []))
            process = set(record.get('process_refs', []))
            if record.get('control') == 'stop':
                self.jev_pending_inputs.clear()
            for n, u in enumerate(utterances, 1):
                if f'I{n}' not in pending and (f'I{n}' in process or record.get('route') == 'background'):
                    self._consume_jev_inputs((u,))
            self._hold_jev_inputs(tuple(u for n, u in enumerate(utterances, 1) if f'I{n}' in pending))
            return
        if record.get('control') == 'stop':
            self.jev_pending_inputs.clear()
        elif record.get('route') == 'wait':
            self._hold_jev_inputs(utterances)
        elif record.get('route') == 'background' or status == 'complete':
            self._consume_jev_inputs(utterances)
        elif status in {'incomplete', 'uncertain'}:
            self._hold_jev_inputs(utterances)

    def _consume_jev_inputs(self, utterances):
        owners = {u.speaker.object_id for u in utterances if u.speaker.method != 'unassigned_audio'}
        self.jev_pending_inputs = {iid:u for iid,u in self.jev_pending_inputs.items()
            if u.speaker.object_id not in owners}

    def _apply_attribution(self, utterances, items, *, persist=True):
        updated = []
        for index, utterance in enumerate(utterances, start=1):
            item = next((it for it in items if it.n == index), None)
            annotated = self.annotations.get(utterance.input_id)
            speaker = annotated if annotated and annotated.method == 'manual_annotation' else utterance.speaker
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
                allowed.update(r['object_id'] for r in self.input_records.get(utterance.input_id,{}).get('personal_classifier',{}).get('ranking',[]))
                if oid in allowed and item.speaker_level in {"确定", "可能"}:
                    profile = self.process.profiles.get(oid)
                    if profile is not None:
                        speaker = replace(speaker, object_id=oid, label=profile.label,
                            method="jev_attribution", status="recognized" if item.speaker_level == "确定" else "provisional")
                elif (item.speaker_pick in {"unknown", "new"} and item.speaker_reason
                      and (initial.method == "voiceprint_match" or speaker.method == 'personal_classifier')):
                    speaker = replace(speaker, object_id="input:" + utterance.input_id, label="声音归属待定",
                        method="unassigned_audio", status="unknown", confidence=None, voiceprint_id="")
                if persist:
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
            display=self.identity_display(u.input_id,u.speaker,item.speaker_level if completed else '')
            await self._archive_update(u.input_id,identity_display=display,jev_final={
                'speaker':asdict(u.speaker),'certainty':item.speaker_level if completed else '',
                'reason':item.speaker_reason if completed else record.get('reason','')})
            await self.emit("identity_final", input_id=u.input_id, speaker=asdict(u.speaker),
                identity_display=display,
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

    def _single_jev_context(self, delivery, context, utterances, cutoff):
        """Already available scene and portraits for the legacy fallback only."""
        result = {**{k:v for k,v in delivery.items() if k != 'jev_scene'}, 'scene':context}
        scene, budget = [], 2000
        for block in reversed(self.process.zone_store.blocks(self.subject_id)):
            if block.at and block.at.timestamp() * 1000 > cutoff:
                continue
            if len(block.text) > budget:
                continue
            scene.append({'text':block.text, 'at':block.at.timestamp()*1000 if block.at else None,
                          'origin':block.origin})
            budget -= len(block.text)
            if len(scene) == 8:
                break
        result['main_scene'] = list(reversed(scene))
        ids = tuple(u.speaker.object_id for u in utterances)
        portraits = self.direct_replies.portraits(self.subject_id, ids, cutoff_ms=cutoff)
        result['person_portraits'] = [{'p':self.assign_code(oid, self.code_labels.get(oid, '')),
            'candidate_only':True, **row} for oid,row in portraits.items()]
        trigger = self.input_records.get(utterances[-1].input_id, {}).get('collection_trigger', '') if utterances else ''
        result['collection'] = {'trigger':trigger,
            'expression_complete':None if trigger in {'', 'ready'} else trigger == 'pause',
            'chars':sum(ch.isalnum() for u in utterances for ch in u.text)}
        return result

    def _shared_scene_revision(self):
        return self.process.pending_scene.shared_revision(self.subject_id,
            since_ms=self.session_started_at_ms - 300000)

    def _sync_scene_updates(self):
        # Only a recent window is loaded when a connection starts. Subsequent
        # updates remain available for its lifetime, even after main writes.
        events = self.process.pending_scene.shared_events(
            self.subject_id, since_ms=self.session_started_at_ms - 300000)
        self.jev_scene_store.sync_shared_events(events)
        return tuple(event['id'] for event in events)

    async def _judge(self, utterances, speaker, overlap, envelope=None) -> tuple[InterruptDecision, dict, tuple]:
        """A judgement invalidated only by scene bookkeeping or its own stop is judged once more."""
        decision, record, items = await self._judge_once(utterances, speaker, overlap, envelope)
        causes = set(record.get('stale_causes') or ())
        own_stop = record.get('control_applied') and record.get('applied_epoch') == self.epoch
        if (causes and causes <= {'generation', 'epoch'} and ('epoch' not in causes or own_stop)
                and not self.closed):
            self.record('voice_jev_stale_retry', {'input_ids': record.get('input_ids', []),
                'causes': sorted(causes), 'first_activity_id': record.get('prompt_activity_id', '')})
            applied = record.get('applied_epoch') if record.get('control_applied') else None
            decision, record, items = await self._judge_once(utterances, speaker, overlap, envelope)
            record['stale_retry_of'] = sorted(causes)
            if applied is not None and not record.get('control_applied'):
                record.update(control_applied=True, applied_epoch=applied)
        return decision, record, items

    async def _judge_once(self, utterances, speaker, overlap, envelope=None) -> tuple[InterruptDecision, dict, tuple]:
        preparation_mono, preparation_wall = monotonic(), time()*1000
        # Hide cold TTS transport setup under semantic judgment. It sends no text
        # and never authorizes speech; actual synthesis still follows JEV/control.
        warmer = getattr(self.cloud, 'prepare_synthesis', None)
        if (warmer and any(u.speaker.method != 'visual_scene' for u in utterances)
                and self.delivery.snapshot().get('state') not in {'playing','paused','queued'}
                and (getattr(self, '_tts_warming', None) is None or self._tts_warming.done())):
            async def prepare_voice():
                try:
                    await warmer()
                except Exception as exc:
                    self.record('voice_tts_prepare', {'status':'deferred','error_type':type(exc).__name__})
            self._tts_warming = asyncio.create_task(prepare_voice())
            self.background_tasks.add(self._tts_warming)
            self._tts_warming.add_done_callback(self.background_tasks.discard)
        schedule_scope = self.entry_scheduler.schedule.capture()
        entry_job = self.entry_scheduler.current
        if entry_job and tuple(u.input_id for u in utterances) != tuple(u.input_id for u in entry_job.rows):
            entry_job = None  # Standalone/legacy callers do not belong to a different in-flight voice job.
        await self.emit('flow_phase', lane='input', stage='prepare')
        from jshi.voice.jev import BatchItem
        snapshot = self.delivery.snapshot()
        if self.active_turn and (not snapshot or snapshot.get("state") in {"completed", "stopped", "failed"}):
            snapshot = self.active_turn
        context, spoken = self._jev_context(utterances)
        shared_revision = self._sync_scene_updates()
        material = self.jev_scene_store.material_view(
            min((u.received_at_ms for u in utterances if u.received_at_ms is not None), default=time()*1000),
            {u.input_id for u in utterances}, knowledge_cutoff_ms=time()*1000)
        # Held half-sentences may already be summarized, so retain those separately.
        held = [r for r in context if r.get('entry_status') == 'pending_continuation']
        context = list(material['events'])
        present = {r.get('input_id') for r in context}
        context.extend(r for r in held if r.get('input_id') not in present)
        names = ["匠石"]
        if hasattr(self.process.profiles, "all_terms"):
            names.extend(term for term in self.process.profiles.all_terms("calls_subject") if term not in names)
        # 还没有交付记录时，状态是空的。空状态不能当成正在播放，
        # 否则没有模型的初判会一律选择继续播放，话进不了主流程。
        state = (snapshot or {}).get("state") or "completed"
        delivery = {"state": state, "spoken": spoken[-1200:],
                    "pending": (snapshot or {}).get("pending_text", "")[:200], "names_for_jiangshi": names,
                    "attention": self.attention.hint(speaker, overlap=overlap),
                    "short_chat": self.direct_replies.context(self.subject_id, [u.speaker.object_id for u in utterances], allow_general=getattr(self.jev, "mode", "legacy") != "legacy"),
                    "jev_scene": material['scene'],
                    "jev_material_scope": {k: material[k] for k in ('version', 'generation', 'archived_unread_count')},
                    "people": {p: self.code_labels.get(oid, p) for p, oid in self.code_owner.items()},
                    "now": time()*1000}
        if self.active_turn is not None and state == 'completed':
            delivery['state'] = 'thinking'
        cutoff = min((u.received_at_ms for u in utterances if u.received_at_ms is not None), default=time()*1000)
        from jshi.core.person_names import known_names
        with stage(self.record, 'jev_known_names', input_ids=[u.input_id for u in utterances]):
            delivery['named_people'] = known_names(self.code_owner, self.process.profiles)
        from jshi.recognition.acquaintance import acquaintance_store
        acquaintances = acquaintance_store(self.process)
        with stage(self.record, 'jev_acquaintance', input_ids=[u.input_id for u in utterances]):
            delivery['acquaintance'] = {self.assign_code(u.speaker.object_id,u.speaker.label):
                acquaintances.context(self.subject_id,u.speaker.object_id) for u in utterances
                if u.speaker.method not in {'unassigned_audio','visual_scene','voice_scene'}}

        if self.utterance_material:
            delivery['utterance_context'] = self.utterance_organizer.material()
        delivery['conversation'] = self.dialogue.recent(self.subject_id, cutoff=cutoff,
            exclude={u.input_id for u in utterances}, session=self.session_id)
        delivery = self.dialogue.clean(delivery, subject=self.subject_id)
        delivery['answer_handling'] = self._answer_handling_context()
        from jshi.voice.wait_ack import model_key
        delivery['waiting'] = self.wait_ack_timing.guidance(model_key(self.process),
            allowed=state == 'completed' and not overlap and not self.pending_voice and not self.output_disconnected
                    )
        if delivery['short_chat']['allowed']:
            delivery['short_chat']['person_code'] = self.assign_code(speaker.object_id, speaker.label)
        if envelope is None:
            source = 'web_text' if utterances and all(u.speaker.method == 'user_selected' for u in utterances) else 'voice'
            parts = (InputPart('text', self.batch_text(utterances)),)
            if source == 'voice' and utterances:
                parts += (InputPart('audio', reference=f'session:{self.session_id}:{utterances[0].start_ms}-{utterances[-1].end_ms}', media_type='audio/pcm'),)
            envelope = InputEnvelope(self.session_id, source, parts, speaker, current_utterances=tuple(utterances))
        visual = getattr(self.process, 'vision', None)
        input_prepare_started = monotonic()
        def prepare_input():
            packed = pack_input(envelope, visual, self.subject_id)
            return packed, packet(packed, visual, self.subject_id)
        # Snapshot reads can wait behind a photo write; keep audio/socket handling live.
        envelope, delivery['envelope'] = await asyncio.to_thread(prepare_input)
        environment = delivery['envelope'].get('environment')
        if environment:
            for analysis in environment.get('analysis', ()):
                for face in (analysis.get('result') or {}).get('faces', ()):
                    oid = face.get('object_id')
                    if oid:
                        profile = self.process.profiles.get(oid)
                        face['person'] = self.assign_code(oid, profile.label if profile else '')
        input_prepare_ms = round((monotonic() - input_prepare_started) * 1000)
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
            if row['source'] == 'web_text':
                row['speaker'] = {'method': 'user_selected'}
            if row['source'] == 'visual_environment':
                row.update(who='unknown', candidates=[], voice_evidence={'strength':'unavailable','basis':'视觉观察，无声音身份'})
        if not batch:
            batch = [{'n': 1, 'text': envelope.text, 'who': 'unknown', 'at': time()*1000, 'source': envelope.source, 'voice_evidence': {'strength':'unavailable','basis':'非声音观察'}}]
        from jshi.voice.jev_material import task_context
        with stage(self.record, 'jev_task_context', input_ids=[u.input_id for u in utterances]):
            delivery['task_context'] = await asyncio.to_thread(task_context, self.process, self.subject_id,
                [u.speaker for u in utterances], code_of=lambda oid: self.assign_code(oid, self.code_labels.get(oid, '')))
        started = monotonic()
        jev_started_at_ms = time()*1000
        report_safely(self.record, 'voice_input_stage', {'stage': 'jev_material_prepare',
            'input_ids': [u.input_id for u in utterances],
            'started_at_ms': preparation_wall, 'finished_at_ms': jev_started_at_ms,
            'elapsed_ms': round((started-preparation_mono)*1000, 3)})
        await self.emit('flow_phase', lane='input', stage='jev', input_ids=[u.input_id for u in utterances if u.input_id])
        for u in utterances:
            if u.input_id in self.input_records:
                record = self.input_records[u.input_id]
                record['jev_started_at'] = started
                record.setdefault('jev_first_started_at', started)
                record['jev_rounds'] = record.get('jev_rounds', 0) + 1
                self.stamp_pipeline(u.input_id, jev_started_at_ms=jev_started_at_ms)
        for row, utterance in zip(batch, utterances):
            row['overlap'] = utterance.overlap
            source = self.input_records.get(utterance.input_id, {}).get('transcript')
            try:
                scope = json.loads(source.identity_note).get('person_scope') if source is not None else None
            except (ValueError, TypeError, AttributeError):
                scope = None
            if scope == 'unaligned_cloud_span' and utterance.speaker.method not in {'manual_annotation', 'user_selected'}:
                row.update(who='unknown', candidates=[], joint_evidence={}, person_scope=scope,
                           voice_evidence={'strength': 'unavailable', 'basis': '云端整段尚未完成按声音分人，不确认整段人物'})
        path, timed_out = "model", False
        prompt_activity_id = "jev-" + uuid4().hex
        sent_requests = []

        def capture_request(request):
            sent_requests.append(request)
            self.process._record_step_input(request, self.jev.model, subject_id=self.subject_id,
                                            activity_id=prompt_activity_id)

        def open_batch(reason):
            # Timeout / concurrent-busy are also JEV-unavailable: do not fail-open into respond.
            action, hold_reason = VoiceJEV._unavailable_hold(delivery, batch_reason=reason)
            items = tuple(BatchItem(int(item["n"]), "no", 0.5, reason,
                                    relevance="unrelated") for item in batch)
            return BatchDecision(action, hold_reason, items, "", False, model_error=reason)

        self.jev_running = {f for f in self.jev_running if not f.done()}
        loop = asyncio.get_running_loop()

        validity = {'epoch': self.epoch, 'generation': material['generation'], 'expired': False}
        entry_result = None

        def same_sources():
            for utterance in utterances:
                source = self.input_records.get(utterance.input_id, {})
                latest = source.get('transcript')
                from jshi.voice.transcripts import equivalent_input_revision
                if source.get('superseded_by') or (latest is not None and not equivalent_input_revision(latest.text, utterance.text)):
                    return False
            return True

        own_inputs = {u.input_id for u in utterances}

        def is_current():
            return ((entry_job is None or entry_job.valid) and same_sources() and not validity['expired'] and not self.closed
                    and self._epoch_current(validity['epoch'], own_inputs)
                    and self.jev_scene_store.state['generation'] == validity['generation']
                    and self._shared_scene_revision() == shared_revision)

        async def apply_entry(result):
            nonlocal entry_result
            if entry_job is not None and not await entry_job.permit():
                return False
            if not is_current():
                return False
            entry_result = result
            if result.control in {'stop', 'redirect'}:
                await self.stop('semantic_' + result.control)
                validity['epoch'] = self.epoch
                entry_result = replace(result, control_applied=True, applied_epoch=self.epoch)
                return self.epoch
            return None

        def on_entry(result):
            return asyncio.run_coroutine_threadsafe(apply_entry(result), loop).result()

        from jshi.voice.jev_decision import entry_bridge
        delivery = {**delivery, '_decision_entry': entry_bridge(self.decisions),
                    'interaction_origin': getattr(self, 'jev_interaction_origin', None)}
        from copy import copy
        gate_snapshot = copy(self.jev)
        gate_snapshot._cooldown_owner = self.jev

        def run():
            if hasattr(self.jev, "decide_batch"):
                if isinstance(self.jev, VoiceJEV) and type(self.jev).decide_batch is VoiceJEV.decide_batch:
                    return gate_snapshot.decide_batch(self.subject_id, batch, context, delivery,
                                                 overlap=overlap, on_request=capture_request, on_entry=on_entry, is_current=is_current)
                return self.jev.decide_batch(self.subject_id, batch, context, delivery, overlap=overlap)
            legacy_delivery = self._single_jev_context(delivery, context, utterances, cutoff)
            decision = self.jev.decide(self.subject_id, "\n".join(item["text"] for item in batch),
                                       asdict(speaker), legacy_delivery, overlap=overlap)
            return VoiceJEV()._from_legacy(decision, batch)

        if len(self.jev_running) >= self.jev_workers:
            path = "rule"
            result = open_batch("上一次初判尚未结束")
        else:
            self.jev_future = loop.run_in_executor(self.jev_executor, run)
            self.jev_running.add(self.jev_future)
            try:
                limit = self.timeout_s * (2 if getattr(self.jev, 'mode', 'legacy') in {'split', 'serial', 'decision'} else 1)
                result = await asyncio.wait_for(asyncio.shield(self.jev_future), limit)
            except asyncio.TimeoutError:
                path, timed_out = "timeout_rule", True
                validity['expired'] = True
                if entry_result is not None:
                    result = replace(entry_result, action='stop' if entry_result.control == 'stop' else 'ignore',
                        route='background', direct_reply='', waiting_reply='', model_error='JEV1_2超时，保留入口判断')
                else:
                    result = open_batch("未能初判")
        # A correction arriving during the model call invalidates its result,
        # even if the asynchronous writer has not run yet.
        self._sync_scene_updates()
        stale_causes = [cause for cause, failed in (
            ('source', not same_sources()), ('closed', self.closed),
            ('entry_job', entry_job is not None and not entry_job.valid),
            ('epoch', not self._epoch_current(validity['epoch'], own_inputs)),
            ('generation', self.jev_scene_store.state['generation'] != validity['generation'])) if failed]
        if set(stale_causes) - {'entry_job'}:
            result = replace(result, action='ignore', control='keep', route='background',
                             direct_reply='', waiting_reply='', items=(), judged=False,
                             routing_error=result.routing_error or 'JEV结果过期，不提交回应')
        validity['expired'] = True
        result = VoiceJEV.enforce_completeness(result)
        decision = InterruptDecision(result.action, result.reason, result.claimed_name)
        await self.emit('flow_phase', lane='input', stage='judged', input_ids=[u.input_id for u in utterances if u.input_id])
        record = {"ms": round((monotonic() - started) * 1000), "action": decision.action,
                  "input_prepare_ms": input_prepare_ms,
                  "path": path, "timed_out": timed_out, "reason": decision.reason[:80],
                  "items": [asdict(item) for item in result.items], "needs_main_review": result.needs_main_review,
                  "input_ids": [u.input_id for u in utterances],
                  "main_prompt_hint": result.main_prompt_hint, "prompt_policy_version": 6,
                  "identity_only": bool(result.items) and all(item.identity_only for item in result.items),
                  "prompt_activity_id": prompt_activity_id,
                  "prompt_requested": bool(sent_requests) if type(self.jev) is VoiceJEV else None,
                  "judged": result.judged, "model_error": result.model_error}
        record.update(entry_route=result.entry_route, interaction=result.interaction,
                      process_refs=list(result.process_refs), pending_refs=list(result.pending_refs),
                      stages=list(result.stage_records), control_applied=result.control_applied,
                      applied_epoch=result.applied_epoch, silent_operation=result.silent_operation,
                      mode=result.stage_records[0]['mode'] if result.stage_records else getattr(self.jev, 'mode', 'legacy'))
        record['routing_error'] = result.routing_error
        failed_stage = next((s for s in reversed(result.stage_records) if s.get('status') == 'failed'), {})
        record['failure_kind'] = 'timeout' if timed_out else failed_stage.get('error_kind', 'unclassified' if result.model_error else '')
        record['failure_stage'] = failed_stage.get('stage', '')
        if record['failure_kind'] == 'stale' or set(stale_causes) - {'entry_job'}:
            record['stale_causes'] = stale_causes
        record['control'] = result.control
        record['utterance_complete'] = result.utterance_complete
        record['decision_reason'] = result.decision_reason
        if result.interaction == 'direct' and not result.routing_error and result.judged:
            # Store only a human-originated contact that JEV actually accepted.
            # Assistant playback, inferred scene summaries and mere topic overlap
            # cannot bootstrap a conversation. This state is connection-local.
            from jshi.voice.jev_serial import voice_interaction_origin
            origin = voice_interaction_origin({
                'lines': [{'text': u.text, 'source': 'web_text' if u.speaker.method == 'user_selected'
                           else 'visual_environment' if u.speaker.method == 'visual_scene' else 'voice'} for u in utterances],
                'state': {'names': names}, 'environment': delivery.get('environment'),
                'availability': {'image_available': bool((delivery.get('environment') or {}).get('image_available'))}})
            if origin and result.control != 'stop':
                self.jev_interaction_origin = {**origin, 'at_ms': cutoff}
            for u in utterances:
                if u.speaker.method not in {'unassigned_audio','visual_scene','voice_scene'}:
                    acquaintances.record(self.subject_id, 'encounter:'+self.session_id+':'+u.speaker.object_id,
                        u.speaker.object_id, 'encounter', {'session':self.session_id,'input_id':u.input_id,
                            'text':u.text,'source':u.speaker.method})

        record['reason'] = decision.reason
        playing = self.delivery.snapshot().get('state') in {'playing', 'paused', 'queued'} or self.active_turn is not None
        from jshi.voice.jev import promote_withheld_stop
        if promote_withheld_stop(record, playing):
            record['stop_promoted'] = True
            record['control'] = 'stop'
            record['route'] = 'background'
            record['action'] = 'stop'
            decision = InterruptDecision('stop', record['decision_reason'] or 'withheld stop')
            result = replace(result, control='stop', action='stop', route='background', reason=decision.reason)
        await self.emit('jev_completeness', utterance_complete=result.utterance_complete, control=record['control'],
                        route=record.get('route') or result.route, input_ids=[u.input_id for u in utterances],
                        failed=bool(result.model_error), at_ms=time()*1000)
        record['direct_reply'] = result.direct_reply if not timed_out else ''
        record['waiting_reply'] = result.waiting_reply if not timed_out else ''
        record['front_choice'] = result.front_choice if not timed_out else 'background'
        record['route'] = record.get('route') or result.route
        record['decision_refs'] = list(result.decision_refs)
        record['shared_scene_revision'] = list(shared_revision)
        record['entry_support'] = list(result.entry_support)
        record['visual_snapshot_id'] = envelope.visual_snapshot_id
        record['input_parts'] = [asdict(p) for p in envelope.parts]
        self.record('voice_jev_diagnostics', {**record, 'model_raw': result.model_raw,
            'model_error': result.model_error})
        jev_finished_at_ms = time()*1000
        for u in utterances:
            if u.input_id in self.input_records:
                self.input_records[u.input_id]['jev_finished_at'] = monotonic()
                self.stamp_pipeline(u.input_id, jev_finished_at_ms=jev_finished_at_ms)
        await self.publish_pipeline(u.input_id for u in utterances if u.input_id)
        if (timed_out or result.model_error) and monotonic() - self._last_jev_notice >= 30:
            self._last_jev_notice = monotonic()
            categories = {'billing':'计费失败', 'authentication':'认证或权限失败', 'rate_limit':'限流',
                          'timeout':'超时', 'transport':'网络连接失败', 'provider_http':'服务请求失败',
                          'output_json':'JSON解析失败', 'output_schema':'输出格式不符合约定',
                          'routing':'路由校验失败', 'stale':'材料已过期', 'cooldown':'接口冷却中'}
            message = ('入口判断'+categories.get(record['failure_kind'], '失败')+
                       '；本轮不开口，原话与诊断记录已保留。')
            await self.emit('notice', text=message)
        elif result.routing_error and monotonic() - self._last_jev_notice >= 30:
            self._last_jev_notice = monotonic()
            await self.emit('notice', text=result.routing_error)
        schedule = self.entry_scheduler.schedule
        matter = schedule.current()
        if (self.decisions.config['mode'] == 'active' and matter and not matter.cancelled
                and schedule_scope.input_version == matter.input_version
                and not result.model_error and not result.routing_error and result.judged
                and result.route != 'wait'):
            attempt = matter.attempts.get(matter.current_attempt)
            if attempt:
                effect = schedule._reapprove(matter, attempt, schedule_scope)
                # New answers may start after this decision; old ones only resume on keep.
                if result.control == 'keep':
                    await self.apply_schedule_effect(effect)
        return decision, record, result.items

    @input_stage('speaker_audio_prepare')
    async def _prepare_speaker_audio(self, batch):
        """Replace a mixed batch only after all independent sources are ready."""
        if self.sample_source is None or any(t.input_id in self.annotations for t in batch):
            return batch
        try:
            prepared = await self.audio_preparer.prepare(batch, self.sample_source)
        except Exception as exc:
            self.record('voice_source_preparation', {'status':'fallback', 'detail':str(exc),
                'input_ids':[t.input_id for t in batch]})
            await self.emit('notice', text='声音分离未完成，保留原始发言：'+str(exc))
            from jshi.voice.sources import original_sources
            prepared = original_sources(batch, self.sample_source)
        if all(kind != 'separated' for _, _, kind in prepared):
            output = []
            for t, pcm, kind in prepared:
                t = replace(t, audio_kind=kind)
                row = self.input_records.get(t.input_id)
                if row is not None:
                    row.update(transcript=t, audio_kind=kind)
                output.append(t)
            return output
        parents = [t.input_id for t in batch]
        base = dict(self.input_records.get(parents[0], {}))
        output = []
        for t, pcm, kind in prepared:
            iid = uuid4().hex
            t = replace(t, input_id=iid, audio_kind=kind)
            if self.local_speakers is not None:
                if self.candidate_future is None or self.candidate_future.done():
                    from functools import partial
                    self.candidate_future = asyncio.get_running_loop().run_in_executor(
                        self.candidate_executor, partial(self.sample_source.match, t, self.local_speakers, pcm=pcm))
                    self.candidate_future.add_done_callback(lambda f:f.exception() if not f.cancelled() else None)
                    try:
                        t = await asyncio.wait_for(asyncio.shield(self.candidate_future), self.candidate_timeout_s)
                    except Exception:
                        t = replace(t, identity_uncertain=True)
            self.input_records[iid] = {**{k:base[k] for k in ('at','received_at_ms','gap_before','asr_lag_ms') if k in base},
                'transcript':t, 'pcm':pcm, 'audio_kind':kind, 'source_inputs':parents}
            self.sample_bytes += len(pcm)
            archive = getattr(self.sample_source, 'archive', None)
            if archive:
                try:
                    capture = 'source-'+iid
                    await asyncio.to_thread(archive.append, capture, t.start_ms*32, pcm)
                    await asyncio.to_thread(archive.save_input, iid, capture, asdict(t), pcm=pcm,
                        received_at_ms=base.get('received_at_ms'))
                    await self._archive_update(iid, source_inputs=parents, audio_kind=kind,
                        original_capture=self.sample_source.capture_id)
                    self.input_records[iid]['audio_saved'] = True
                except Exception:
                    logging.getLogger(__name__).exception('Extracted source archive failed')
            output.append(t)
        for t in batch:
            row = self.input_records.get(t.input_id)
            if row is not None:
                row['superseded_by'] = [u.input_id for u in output]
                await self._archive_update(t.input_id, separated_inputs=row['superseded_by'])
        self.record('voice_source_preparation', {'status':'separated', 'input_ids':parents,
            'outputs':[t.input_id for t in output], 'speakers':len({t.track_id for t in output})})
        return output

    async def _work(self) -> None:
        while True:
            transcript = self.input_carry if self.input_carry is not None else await self.queue.get()
            self.input_carry = None
            collect_at_ms = time()*1000
            await self.emit('flow_phase', lane='input', stage='collect')
            if getattr(transcript, 'input_id', None):
                self.stamp_pipeline(transcript.input_id, collect_at_ms=collect_at_ms)
            worker_started = monotonic()
            batch = [transcript]
            queued_count = None
            collection_trigger = 'capacity'
            try:
                # Drain only material already available. Never wait for a later
                # transcript, punctuation, character count or a text-idle timer.
                # An in-flight accept_batch only finishes recording an ASR batch
                # whose complete results have already arrived.
                async with self.accepting_batch:
                    while not self.queue.empty() and len(batch) < 64:
                        batch.append(self.queue.get_nowait())
                active_control = self.active_turn is not None or self.delivery.snapshot().get('state') in {'playing', 'paused', 'queued'}
                collection_trigger = 'active_control' if active_control else 'ready'
                expression_complete = False if active_control else None
                queued_count = len(batch)
                for item in batch:
                    self.stamp_pipeline(item.input_id, collect_at_ms=collect_at_ms)
                batch.sort(key=lambda t: (t.start_ms, t.end_ms))
                if collection_trigger != 'active_control':
                    batch = await self._prepare_speaker_audio(batch)
                group_ready = monotonic()
                binding_notes = []
                current = []
                from jshi.voice.jev import explicit_name
                for transcript in batch:
                    input_record = self.input_records.get(transcript.input_id, {})
                    if input_record.get('superseded_by'):
                        continue
                    transcript = input_record.get('transcript', transcript)
                    input_record['processing_started'] = True
                    report_safely(self.record, 'voice_input_stage', {'stage': 'input_worker_started',
                        'input_ids': [transcript.input_id],
                        'received_to_worker_ms': round((monotonic()-input_record['at'])*1000, 3)})
                    input_record["group_ready_at"] = group_ready
                    input_record['collection_trigger'] = collection_trigger
                    input_record["worker_started_at"] = max(worker_started, input_record.get("at", worker_started))
                    identity_started = monotonic()
                    prior_cluster = self.identities.clusters.get(transcript.speaker_cluster_id) if transcript.speaker_cluster_id else None
                    with stage(self.record, 'resolve_speaker', input_ids=[transcript.input_id]):
                        evidence = self.resolve_speaker(transcript)
                    self.source_speakers[transcript.input_id] = evidence
                    if (prior_cluster and prior_cluster.object_id != evidence.object_id
                            and evidence.method == 'voiceprint_match' and evidence.status == 'recognized'
                            and not transcript.overlap and not transcript.identity_uncertain):
                        old_profile=self.process.profiles.get(prior_cluster.object_id)
                        if old_profile and old_profile.source=='voice_anonymous':
                            related=[iid for iid,src in self.source_speakers.items()
                                if src.object_id==prior_cluster.object_id
                                and self.input_records.get(iid,{}).get('transcript') is not None
                                and self.input_records[iid]['transcript'].speaker_cluster_id==transcript.speaker_cluster_id
                                and not self.input_records[iid]['transcript'].overlap]
                            if related:
                                self.link_selected_inputs(related,evidence.object_id,basis='verified_voice')

                    # Start personal classifier without blocking the first on-screen text.
                    evidence = await self.personal_identity(transcript, evidence, wait=False)
                    received = self.input_records.get(transcript.input_id, {}).get('received_at_ms')
                    origin = self.sample_source.started_at_ms if self.sample_source else None
                    utterance = SceneUtterance(transcript.text, evidence, transcript.start_ms, transcript.end_ms, transcript.overlap, transcript.input_id,
                        origin + transcript.start_ms if origin is not None else None,
                        origin + transcript.end_ms if origin is not None else None, received, transcript.identity_note)
                    from jshi.voice.latency import public_timing
                    input_record['text_sent_mono'] = monotonic()
                    input_record.setdefault('asr_timing', {})['_text_sent_mono'] = input_record['text_sent_mono']
                    input_record['asr_timing'].setdefault('_asr_return_mono', input_record['at'])
                    self.stamp_pipeline(transcript.input_id, text_sent_at_ms=time()*1000)
                    # Show text first; identity and candidates refine the same row afterwards.
                    await self.emit("timeline", **asdict(utterance), voiceprint_candidates=[],
                                    voice_latency=public_timing(input_record['asr_timing']),
                                    pipeline=dict(input_record.get('pipeline') or {}),
                                    identity_display=self.identity_display(transcript.input_id,evidence),
                                    audio_saved=input_record.get('audio_saved',False),
                                    identity_comparison=self.identity_comparison(transcript.input_id),
                                    personal_prediction=self.personal_prediction_view(input_record))
                    latency = await self.save_voice_latency(transcript.input_id, 'text_sent')
                    if not input_record.get('display_acknowledged'):
                        await self._archive_update(transcript.input_id, voice_latency=latency)
                    # Both evidence lanes begin before either is awaited. Keep
                    # voiceprint results even when MLP later recognizes someone.
                    candidate_started = monotonic()
                    if collection_trigger == 'active_control':
                        found, candidate_status = [], '控制判断使用已有证据，不等待额外声音模型'
                        evidence = await self.personal_identity(transcript, evidence, wait=False)
                    else:
                        evidence, candidate_result = await asyncio.gather(
                            self.personal_identity(transcript, evidence),
                            self.rank_candidates(transcript, evidence))
                        found, candidate_status = candidate_result
                    input_record['candidate_ms'] = round((monotonic()-candidate_started)*1000)
                    answer = self.identities.name_answer(evidence, transcript.text) if not transcript.overlap else ''
                    claimed = (explicit_name(transcript.text) or answer) if not transcript.overlap else ""
                    if self.identities.acquaintance is not None and evidence.method not in {'unassigned_audio','visual_scene','voice_scene'}:
                        with stage(self.record, 'acquaintance_observe', input_ids=[transcript.input_id]):
                            self.identities.acquaintance.observe_text(self.subject_id,evidence.object_id,transcript.input_id,
                                transcript.text,session=self.session_id,encounter=False)

                    remembered = False
                    if self.local_speakers is not None and evidence.method == 'anonymous_voice_cluster':
                        remembered = await asyncio.to_thread(self.local_speakers.remember_anonymous,
                            transcript.speaker_cluster_id or transcript.track_id, evidence.object_id)
                    if claimed:
                        evidence, binding_note = self.identities.introduce(transcript.track_id, claimed,
                            basis='name_answer' if answer else 'explicit_self_introduction',
                            cluster_id=transcript.speaker_cluster_id, uncertain=transcript.identity_uncertain,input_id=transcript.input_id)
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
                        if (self.local_speakers is not None and evidence.method != 'unassigned_audio'
                                and self.process.profiles.get(evidence.object_id) is not None):
                            # Retain the already associated voice; naming does not add
                            # unverified audio or merge any same-named registration.
                            self.local_speakers.confirm(evidence.object_id)
                        binding_notes.append(binding_note)
                        if evidence.status == "introduced":
                            self.attention.engage((evidence.object_id,))
                        await self.emit("identity", **asdict(evidence), detail=binding_note)
                    if remembered and evidence.label.startswith('未命名访客') and evidence.object_id not in self.remembered_voices:
                        self.remembered_voices.add(evidence.object_id)
                        await self.emit('notice', text=f'{evidence.label} 是内部名，还不知道姓名。声音已暂存；得知称呼后，会记在同一个人下面。')
                    utterance = replace(utterance, speaker=evidence)
                    self.note_address(evidence, transcript.text, transcript.input_id)
                    self.note_person_names(evidence, transcript.text, transcript.input_id)
                    input_record["identity_ms"] = round((monotonic() - identity_started) * 1000)
                    pcm = (self.input_records.get(transcript.input_id) or {}).get("pcm")
                    if self.local_speakers is not None and hasattr(self.local_speakers, 'collect_candidate'):
                        actor = evidence.object_id if evidence.method != 'unassigned_audio' else 'input:' + transcript.input_id
                        input_record['voice_candidate_status'] = self._sample_job(transcript.input_id, 'voice_candidate_collected',
                            lambda pcm=pcm, t=transcript, actor=actor: self.local_speakers.collect_candidate(pcm, t.input_id,
                                actor, self.session_id, overlap=t.overlap, start_ms=t.start_ms, end_ms=t.end_ms))
                    personal = input_record.get('personal_classifier', {})
                    if personal.get('ranking') and not personal.get('matched'):
                        # Preserve both sources; their scores are not ranked against one another.
                        found = [*found, *[{key: row[key] for key in ('object_id', 'label', 'score', 'source')}
                                          for row in personal['ranking'][:3]
                                          if row.get('verified') is not False
                                          and row['object_id'] not in {r['object_id'] for r in found}]]
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
                    utterance = self._latest_utterances([utterance])
                    if not utterance:
                        continue
                    utterance = utterance[0]
                    current.append(utterance)
                    self.last_track = transcript.track_id
                    # Publish separately from identity: even a very weak first place can
                    # preselect a training label, without confirming the speaker.
                    ui_candidates = [{**row, 'source': row.get('source', 'voiceprint')} for row in found if row.get('source', 'voiceprint') in
                                     {'voiceprint', 'voiceprint_match', 'repeated_short_audio'}
                                     and isinstance(row.get('score'), (int, float))]
                    await self._archive_update(transcript.input_id,speaker=asdict(evidence),
                        identity_display=self.identity_display(transcript.input_id,evidence),
                        voiceprint_candidates=ui_candidates,identity_note=utterance.identity_note,
                        identity_comparison=self.identity_comparison(transcript.input_id),
                        classifier_result=self.personal_prediction_view(input_record),
                        voiceprint_check={'status': candidate_status, 'elapsed_ms': input_record['candidate_ms'],
                            'short_repeat': {k:v for k,v in input_record.get('short_voice_hint', {}).items() if k != 'candidates'}})
                    await self.emit("voiceprint_candidates", input_id=transcript.input_id,
                                    candidates=ui_candidates, identity_note=utterance.identity_note,
                                    voiceprint_check={'status': candidate_status, 'elapsed_ms': input_record['candidate_ms']})
                    if self.personal_speakers is not None:
                        job = self.personal_speakers.collect_original(input_record.get('pcm'), transcript,
                            self.source_speakers[transcript.input_id], self.session_id, self.local_speakers,
                            candidates=ui_candidates)
                        if job:
                            task = asyncio.create_task(self._publish_personal_auto(transcript.input_id, job))
                            self.background_tasks.add(task)
                            task.add_done_callback(self.background_tasks.discard)
                    await self.emit("speaker", **asdict(evidence), jev_status="pending")
                if current:
                    current = self._retain_repeated_requests(current)
                if current:
                    # Display never waits for the previous group's entry judgment.
                    # Queue unfinished-task accounting still completes after judgment
                    # so existing tests can await queue.join(); accept backpressure
                    # only waits for free display slots, not for that join.
                    for u in current:
                        report_safely(self.record, 'voice_input_stage', {
                            'stage': 'jev_input_queued', 'input_ids': [u.input_id]})
                    self.judge_pending.append({'current':current, 'track_id':transcript.track_id,
                        'expression_complete':expression_complete, 'collection_trigger':collection_trigger,
                        'queued':queued_count, 'queued_at':monotonic()})
                    self.judge_ready.set()
                    self.entry_scheduler.notify(current)
                    queued_count = 0
            except Exception as exc:
                await self.emit("notice", text=f"语音轮次失败：{type(exc).__name__}：{exc}")
            finally:
                for _ in range(queued_count if queued_count is not None else len(batch)):
                    self.queue.task_done()
                self._refresh_input_idle()

    def _refresh_input_idle(self):
        if (self.queue.empty() and self.input_carry is None
                and not self.judge_pending and not self.judging):
            self.input_idle.set()

    def _take_judge_group(self):
        from jshi.voice.entry_queue import take_group
        names = ['匠石']
        if hasattr(self.process.profiles, 'all_terms'):
            names.extend(self.process.profiles.all_terms('calls_subject'))
        group = take_group(self.judge_pending, names=names)
        self.record('voice_entry_queue', {'input_ids': [u.input_id for u in group['current']],
            'queued_groups': len(self.judge_pending), 'wait_ms': round((monotonic()-group.get('queued_at', monotonic()))*1000)})
        for u in group['current']:
            queued = group['queued_at_by_input'].get(u.input_id)
            report_safely(self.record, 'voice_input_stage', {'stage': 'jev_input_dequeued',
                'input_ids': [u.input_id],
                'queue_wait_ms': round((monotonic()-queued)*1000, 3) if queued is not None else None})
        return group

    async def _judge_work(self) -> None:
        while True:
            await self.judge_ready.wait()
            if not self.judge_pending:
                self.judge_ready.clear()
                continue
            self.judging = True
            group = self._take_judge_group()
            current = group['current']
            expression_complete = group['expression_complete']
            collection_trigger = group['collection_trigger']
            input_epoch = self.epoch
            try:
                current = self._latest_utterances(current)
                if collection_trigger == 'revision':
                    from jshi.voice.transcripts import equivalent_input_revision
                    duplicate = [u for u in current
                        if (previous := self.input_records.get(u.input_id, {}).get('last_judged_revision_text')) is not None
                        and self.input_records[u.input_id].get('last_judged_revision_version')
                            == self.input_records[u.input_id].get('revision_version', 0)
                        and equivalent_input_revision(previous, u.text)]
                    if duplicate:
                        self.record('voice_revision_already_judged', {
                            'input_ids': [u.input_id for u in duplicate],
                            'reason': 'earlier_entry_used_current_revision'})
                        current = [u for u in current if u not in duplicate]
                if not current:
                    continue
                evidence = current[-1].speaker
                active = self.delivery.snapshot().get("state") in {"playing", "paused", "queued"} or self.active_turn is not None
                explicit_stop = any(not u.overlap and VoiceJEV().decide(self.subject_id, u.text, {}, {}).action == 'stop' for u in current)
                busy = self.active_turn is not None and not explicit_stop
                jev_calls = ()
                items = ()
                if explicit_stop:
                    decision = InterruptDecision('stop', 'explicit stop')
                else:
                    judged_sources = tuple(current)
                    judged_versions = {u.input_id: self.input_records.get(u.input_id, {}).get('revision_version', 0)
                                       for u in current}
                    judged = await self.entry_scheduler.run(current,
                        lambda: self._judge(current, evidence, any(u.overlap for u in current)))
                    work_task = self.entry_scheduler.work_task
                    if work_task and not work_task.done() and self.entry_scheduler.applies():
                        # Its effect on old work must land before this batch is queued.
                        await asyncio.shield(work_task)
                    if self._epoch_current(input_epoch, {u.input_id for u in current}):
                        input_epoch = self.epoch
                    effect = self.entry_scheduler.take_effect()
                    if effect and effect.grade == 'restart':
                        self.premise_changed = True
                    elif effect and effect.kind == 'reapprove':
                        self.premise_changed = False
                    if effect and effect.kind == 'cancel':
                        await self.apply_schedule_effect(effect)
                        continue
                    if effect and effect.kind == 'capped':
                        self.entry_scheduler.deferred_group = {**group, 'queued': 0}
                        continue
                    if effect and effect.resume:
                        await self.apply_schedule_effect(effect)
                    if judged is None:
                        # Transfer queue accounting along with the original speech.
                        # The latest input may supersede some rows; existing source checks remove those.
                        self.judge_pending.appendleft(group)
                        group = {**group, 'queued': 0}
                        self.judge_ready.set()
                        continue
                    decision, record, items = judged
                    if input_epoch != self.epoch and record.get("applied_epoch") != self.epoch:
                        continue  # A newer control invalidated this batch.
                    if record.get('judged') and not record.get('stale_causes') and not record.get('model_error'):
                        for u in judged_sources:
                            if u.input_id in self.input_records:
                                self.input_records[u.input_id]['last_judged_revision_text'] = u.text
                                self.input_records[u.input_id]['last_judged_revision_version'] = judged_versions[u.input_id]
                    record['collection_complete'] = expression_complete
                    record['collection_trigger'] = collection_trigger
                    record['collected_chars'] = sum(ch.isalnum() for u in current for ch in u.text)
                    current = self._apply_attribution(current, items, persist=not bool(record.get('direct_reply')))
                    current = self._latest_utterances(current)
                    if not current:
                        continue
                    await self._publish_attribution(current, items, record)
                    jev_calls = (record,)
                # Publish only after entry judgment/rule completion. Background
                # snapshots must never race ahead of this batch's first JEV.
                control = jev_calls[-1].get('control') if jev_calls else ''
                if control in {'stop', 'redirect'} and not record.get('control_applied'):
                    await self.stop('semantic_' + control)
                    input_epoch = self.epoch
                    active = busy = False
                self._capture_jev_inputs(current, items, entry_status='rule' if explicit_stop else None,
                                         interaction=record.get('interaction'))
                if jev_calls:
                    self._update_jev_continuation(current, jev_calls[-1])
                if control == 'stop' and not record.get('silent_operation'):
                    self.jev_pending_inputs.clear()
                    continue
                if control == 'keep' and record.get('route') not in {'main', 'self_reply'}:
                    self._preserve_background(current)
                    continue
                if record.get('control_applied'):
                    input_epoch = self.epoch
                    active = busy = False
                self.pending_voice = False
                if busy:
                    await self.emit('input_pending', count=len(current))
                else:
                    self.last_jev = asdict(decision)
                    self.record("voice_jev", {"input": self.render_input(current, items), "track_id": group['track_id'], **asdict(decision)})
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
                text_only = all(u.speaker.method == 'user_selected' for u in current)
                parts = (InputPart('text', text),)
                if not text_only:
                    parts += (InputPart('audio', reference=f'session:{self.session_id}:{current[0].start_ms}-{max(u.end_ms for u in current)}', media_type='audio/pcm'),)
                envelope = InputEnvelope(
                    session_id=self.session_id, source="web_text" if text_only else "microphone",
                    parts=parts, voice_delivery=text_only,
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
                self.judging = False
                if not self.judge_pending:
                    self.judge_ready.clear()
                for _ in range(group['queued']):
                    self.queue.task_done()
                self._refresh_input_idle()

    async def _turns(self) -> None:
        while True:
            # Each queued envelope is already a completed speech group. New
            # speech must not hold ready groups hostage via a global idle flag.
            envelope, epoch = await self.turn_queue.get()
            # Writing is independent; cognition sees committed scene + pending events.
            write_wait_ms = 0
            pending = [envelope] if epoch == self.epoch else []
            while not self.turn_queue.empty():
                newer, newer_epoch = self.turn_queue.get_nowait()
                if newer_epoch == self.epoch:
                    pending.append(newer)
                self.turn_queue.task_done()
            if not pending:
                self.turn_queue.task_done()
                continue
            epoch = self.epoch
            envelope = self.merge_envelopes(pending)
            self.turn_busy = True
            current = ()
            try:
                if epoch != self.epoch:
                    continue
                if envelope.source == 'visual_environment' and not self.input_idle.is_set():
                    self.defer_environment(envelope)
                    continue
                def annotated(u): return replace(u, speaker=self.identities.associated(self.annotations.get(u.input_id, u.speaker)))
                current = tuple(annotated(u) for u in self._latest_utterances(envelope.current_utterances))
                if envelope.current_utterances and not current:
                    continue
                from jshi.voice.jev import BatchItem
                items = ()
                if envelope.deferred_interrupt:
                    decision, record, items = await self._judge(current, envelope.speaker, envelope.overlap, envelope)
                    if record.get('applied_epoch') is not None:
                        if record['applied_epoch'] != self.epoch:
                            continue
                        epoch = self.epoch
                    current = tuple(self._apply_attribution(list(current), items, persist=not bool(record.get('direct_reply'))))
                    await self._publish_attribution(current, items, record)
                    envelope = replace(envelope, jev_calls=(*envelope.jev_calls, record),
                        visual_snapshot_id=record.get('visual_snapshot_id', ''),
                        parts=tuple(InputPart(**p) for p in record.get('input_parts', ())))
                    await self.emit('jev', **asdict(decision))
                    self.last_jev = asdict(decision)
                    self.record('voice_jev', {'input': self.render_input(current, items), **asdict(decision)})
                    if record.get('control') in {'stop', 'redirect'} and not record.get('control_applied'):
                        await self.stop('semantic_' + record['control'])
                        epoch = self.epoch
                    self._capture_jev_inputs(current, items)
                    self._update_jev_continuation(current, record)
                    if record.get('control') == 'stop' and not record.get('silent_operation'):
                        continue
                    if record.get('control') == 'keep' and record.get('route') not in {'main', 'self_reply'}:
                        self._preserve_background(current)
                        continue
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
                if len(envelope.jev_calls) == 1:
                    call = envelope.jev_calls[0]
                    direct = call.get('direct_reply', '')
                    oid = current[0].speaker.object_id if current else ''
                    if (direct and current and all(u.speaker.object_id == oid for u in current)
                            and set(call.get('input_ids', ())) == {u.input_id for u in current}
                            and (call.get('collection_complete') is not False
                                 or call.get('utterance_complete') == 'complete' and not call.get('pending_refs'))
                            and self.direct_replies.accept(self.subject_id, oid, '\n'.join(u.text for u in current), direct, include_reply=False, allow_general=getattr(self.jev, 'mode', 'legacy') != 'legacy')):
                        await self._direct_reply(direct, oid, epoch, envelope, current, call)
                        continue
                facts = []
                if (envelope.jev_calls and envelope.jev_calls[-1].get('collection_complete') is False
                        and envelope.jev_calls[-1].get('utterance_complete') != 'complete'):
                    facts.append('本轮只是分批处理；尚未观察到发言人的足够停顿，不能据此认为表达已经结束。')
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
                    'speaker_level': marks[n].speaker_level if n in marks else '',
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
                        delivery_context=self.dialogue.note(self.subject_id,
                            cutoff=min((u.received_at_ms for u in current if u.received_at_ms is not None), default=time()*1000),
                            exclude={u.input_id for u in current} | self.process.pending_scene.input_ids(self.subject_id)),
                        voice_delivery=True,
                        parts=(InputPart('text', text), *envelope.parts[1:]))
                if envelope.jev_calls and not envelope.visual_snapshot_id:
                    call = envelope.jev_calls[-1]
                    envelope = replace(envelope, visual_snapshot_id=call.get('visual_snapshot_id', ''))
                    images = tuple(InputPart(**p) for p in call.get('input_parts', ()) if p['kind'] == 'image')
                    envelope = replace(envelope, parts=tuple(p for p in envelope.parts if p.kind != 'image') + images)
                self.entry_scheduler.schedule.set_task('cognition', 'running')
                organized = self.utterance_organizer.material() if self.utterance_material else None
                if organized:
                    envelope = replace(envelope, delivery_context=(envelope.delivery_context or '') +
                        '\n【发言接续记录】以下为声音发言的接续与插话记录，状态是分析证据；未知人物不据此确认身份。\n' +
                        json.dumps(organized, ensure_ascii=False))
                self.active_turn = {"state": "thinking", "object_id": envelope.speaker.object_id,
                                    "input": envelope.text, "pending_text": "尚未生成回应"}
                self.schedule_person_review(current, items)
                plan = await self._respond(envelope, epoch)
                await self.emit('turn_outcome', source=envelope.source, mode=plan.mode,
                                replied=bool(plan.verbal_text()), reason=plan.reason[:200])
            except TurnCancelled:
                await self.emit('notice', text='上一轮认知已中断，旧回复已作废。')
            except Exception as exc:
                await self.emit("notice", text=f"主流程调用失败：{exc}")
            finally:
                self.turn_busy = False
                self.active_turn = None
                self.entry_scheduler.schedule.set_task('cognition', 'done')
                self.last_settled_at_ms = time()*1000
                complete_ids = [u.input_id for u in (current or ()) if getattr(u, 'input_id', None)]
                for key, ids in self.reply_input_ids.items():
                    if set(complete_ids).intersection(ids) and self.answer_status.get(key) == 'running':
                        self.answer_status[key] = 'silent'
                for iid in complete_ids:
                    self.stamp_pipeline(iid, turn_complete_at_ms=self.last_settled_at_ms)
                await self.publish_pipeline(complete_ids)
                await self.emit('turn_complete', input_ids=complete_ids, at_ms=self.last_settled_at_ms)
                self.turn_queue.task_done()

    async def _respond(self, envelope, epoch: int) -> None:
        loop = asyncio.get_running_loop()
        preparation_started = monotonic()
        prepared, preparation_route = await self.preparation.finish(envelope)
        preparation_finish_ms = round((monotonic() - preparation_started) * 1000)
        if self.closed or epoch != self.epoch:
            return
        from jshi.voice.transcripts import equivalent_input_revision
        if any(self.input_records.get(u.input_id, {}).get('superseded_by') or
               (self.input_records.get(u.input_id, {}).get('transcript') is not None and
                not equivalent_input_revision(u.text, self.input_records[u.input_id]['transcript'].text))
               for u in envelope.current_utterances):
            self.record('voice_answer_discarded', {'input_ids': [u.input_id for u in envelope.current_utterances],
                        'reason': 'source_changed_during_preparation'})
            return
        preparation_context = json.dumps(prepared.model_material(include_memories=False), ensure_ascii=False) if prepared else ''
        feedback_revision = self.feedback.revision
        emitted = False
        self.turn_sequence += 1
        turn_key = (epoch, self.turn_sequence)
        self.reply_input_ids[turn_key] = [u.input_id for u in envelope.current_utterances]
        self.answer_status[turn_key] = 'running'
        envelope = replace(envelope, delivery_context='\n'.join(filter(None,
            (envelope.delivery_context, self._answer_handling_context()))))
        while len(self.reply_input_ids) > 32:
            old_key = next(iter(self.reply_input_ids))
            self.reply_input_ids.pop(old_key)
            self.answer_status.pop(old_key, None)
        began = monotonic()
        main_started_at_ms = time()*1000
        for u in envelope.current_utterances:
            self.stamp_pipeline(u.input_id, main_started_at_ms=main_started_at_ms)
        rows = [self.input_records[u.input_id] for u in envelope.current_utterances if u.input_id in self.input_records]
        self._record_stage_latency(envelope, began)
        calls = [*self.unreported_jev, *envelope.jev_calls]
        self.unreported_jev.clear()
        metrics = {'input_to_main_ms': round((began - min(r['at'] for r in rows)) * 1000) if rows else None,
                   'queue_wait_ms': max((round((began-r.get('ready_at', began))*1000) for r in rows), default=None),
                   'asr_lag_ms': max((r['asr_lag_ms'] for r in rows if r.get('asr_lag_ms') is not None), default=None),
                   'jev_ms': sum(call['ms'] for call in calls), 'jev_calls': calls}
        current_ids = {u.input_id for u in envelope.current_utterances}
        metrics['current_jev_ms'] = sum(call['ms'] for call in calls if current_ids.intersection(call.get('input_ids', ())))
        metrics['historical_jev_ms'] = metrics['jev_ms'] - metrics['current_jev_ms']
        if rows:
            first = min(rows, key=lambda r: r['at'])
            current_calls = [call for call in calls if current_ids.intersection(call.get('input_ids', ()))]
            envelope = replace(envelope, entry_timing={
                'received_mono': first['at'], 'received_at_ms': first.get('received_at_ms'),
                'source': envelope.source, 'batch_origin': 'first_current_input_received',
                'input_prepare_ms': sum(call.get('input_prepare_ms', 0) for call in current_calls),
                'jev_ms': metrics['current_jev_ms'],
                'historical_jev_ms': metrics['historical_jev_ms'],
                'ready_queue_wait_ms': max((round((preparation_started - r.get('ready_at', preparation_started)) * 1000)
                                            for r in rows), default=None),
                'preparation_finish_ms': preparation_finish_ms,
                'audio_end_received_at_ms': max((r['pipeline']['audio_end_received_at_ms'] for r in rows
                    if r.get('pipeline', {}).get('audio_end_received_at_ms') is not None), default=None),
            })
        metrics['input_timings'] = [{
            'input_id': u.input_id, 'received_at_ms': r.get('received_at_ms'),
            'estimated_audio_end_at_ms': u.recorded_end_at_ms,
            'audio_end_received_at_ms': r.get('pipeline', {}).get('audio_end_received_at_ms'),
            'collection_wait_ms': round(max(0, r.get('group_ready_at', r['at']) - r['at']) * 1000),
            'identity_ms': r.get('identity_ms', 0), 'candidate_ms': r.get('candidate_ms', 0),
            'jev_ms': round(max(0, r.get('jev_finished_at', r['at']) - r.get('jev_started_at', r['at'])) * 1000),
            'ready_queue_wait_ms': round(max(0, began - r.get('ready_at', began)) * 1000),
        } for u in envelope.current_utterances if (r := self.input_records.get(u.input_id)) is not None]
        metrics['estimated_audio_end_at_ms'] = max((u.recorded_end_at_ms for u in envelope.current_utterances
                                          if u.recorded_end_at_ms is not None), default=None)
        # All current inputs must have anchors for a complete batch endpoint.
        audio_ends = [r.get('asr_timing', {}).get('_audio_end_received_mono') for r in rows]
        audio_end_mono = (max(audio_ends) if len(rows) == len(envelope.current_utterances)
                          and audio_ends and all(t is not None for t in audio_ends) else None)
        metrics.update({"speech_group_wait_ms": max((round((r.get("group_ready_at", r["at"]) - r["at"]) * 1000) for r in rows), default=0),
            'online_identity_ms':sum(r.get('online_identity_ms',0) for r in rows),
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
            if emitted or epoch != self.epoch or self.closed or turn_key in self.retired_turns:
                if not emitted:
                    self.record('voice_answer_discarded', {'turn_key': turn_key, 'input_ids': list(current_ids),
                        'reason': self.retired_turns.get(turn_key, 'closed' if self.closed else 'epoch_changed')})
                return
            if any(c.get('silent_operation') for c in envelope.jev_calls):
                emitted = True
                return
            emitted = True
            actions = [i.text for i in plan.items if i.channel == 'embodied' and i.text.strip() and i.text.strip() != '无动作']
            if actions and epoch == self.epoch and not self.closed:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self.emit('embodied',
                    turn_key=list(turn_key), actions=actions)))
            metrics['model_to_reply_ms'] = round((monotonic() - began) * 1000)
            from jshi.voice.wait_ack import model_key
            level = (envelope.jev_calls[-1].get('main_prompt_hint') or {}).get('level', 1) if envelope.jev_calls else 1
            try:
                self.wait_ack_timing.observe(model_key(self.process), level, metrics['model_to_reply_ms']/1000)
            except OSError:
                logging.getLogger(__name__).exception('Waiting latency statistics could not be saved')
            metrics['main_start_to_reply_ms'] = metrics['model_to_reply_ms']
            metrics['reply_ready_at_ms'] = time() * 1000
            from jshi.voice.latency import elapsed_ms
            reply_mono = monotonic()
            metrics['speech_to_reaction_ms'] = None  # Acoustic speech-end time is not measured.
            metrics['audio_end_received_to_reaction_ms'] = elapsed_ms(reply_mono, audio_end_mono)
            metrics['received_to_reaction_ms'] = elapsed_ms(reply_mono, max((r['at'] for r in rows), default=None))
            # Only enqueue on the event loop. Neither synthesis nor playback
            # blocks cognition or the independent write-zone call.
            valid = {u.speaker.object_id for u in (*envelope.utterances, *envelope.current_utterances)} | {envelope.speaker.object_id}
            items = tuple(ResponseItem(i.channel, i.text, tuple(t for t in (self.object_of(token) for token in i.target_ids) if t in valid))
                          for i in plan.items if i.channel == "verbal" and i.text.strip()
                          and (not i.target_ids or any(self.object_of(t) in valid for t in i.target_ids))) if plan.mode == "respond" else ()
            if not items:
                self.answer_status[turn_key] = 'silent'
                return
            self.answer_status[turn_key] = 'ready'
            from jshi.voice.delivery import sentences
            self.dialogue.plan(self.subject_id, self.session_id + ':' + str(turn_key), self.session_id,
                [segment for item in items for segment in sentences(item.text)])
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._start_plan(items, envelope.speaker.object_id, epoch, turn_key)))

        if len(envelope.jev_calls) == 1:
            call = envelope.jev_calls[0]
            brief = ('我再想一下。' if preparation_route == 'restart' else call.get('waiting_reply', ''))
            if (brief and set(call.get('input_ids', ())) == current_ids
                    and call.get('collection_complete') is not False
                    and not self.pending_voice and not envelope.overlap
                    and not self.output_disconnected
                    and self.delivery.snapshot().get('state') not in {'playing', 'paused', 'queued'}
                    and self.wait_ack_timing.claim_ack()):
                from jshi.voice.wait_ack import ack_context
                envelope = replace(envelope, delivery_context='\n'.join(filter(None,
                    (envelope.delivery_context, ack_context(brief)))))
                ack_key = (epoch, -self.turn_sequence)
                self.reply_input_ids[ack_key] = [u.input_id for u in envelope.current_utterances]
                self.voice_timings[ack_key] = (monotonic(), {'route':'waiting_ack'})
                async def short_feedback():
                    if (emitted or self.closed or epoch != self.epoch or not self.input_idle.is_set() or self.preparation.fragments
                            or not self.feedback.claim('reconsider_ack' if preparation_route == 'restart' else 'waiting_ack',
                                feedback_revision, busy=self.delivery.snapshot().get('state') in {'playing','paused','queued'},
                                speaking=self.speech_active(), overlap=envelope.overlap, enabled=True)):
                        return
                    await self._start_plan((ResponseItem('verbal', brief, (envelope.speaker.object_id,)),),
                                           envelope.speaker.object_id, epoch, ack_key)
                    await self.emit('jev_reply', text=brief, route='waiting_ack', main_called=True, memory_written=False)
                feedback_task = asyncio.create_task(short_feedback())
                self.background_tasks.add(feedback_task)
                feedback_task.add_done_callback(self.background_tasks.discard)
        await self.publish_pipeline(u.input_id for u in envelope.current_utterances if u.input_id)
        await self.emit("thinking", source=envelope.source)
        def phase_ready(stage):
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self.emit('flow_phase',
                lane='main', stage=stage, source=envelope.source,
                input_ids=[u.input_id for u in envelope.current_utterances if u.input_id])))
            if stage == 'reconsider':
                async def reconsider_feedback():
                    if (emitted or self.closed or epoch != self.epoch or not self.input_idle.is_set() or self.preparation.fragments
                            or not self.wait_ack_timing.claim_ack()
                            or not self.feedback.claim('reconsider_ack', feedback_revision,
                                busy=self.delivery.snapshot().get('state') in {'playing','paused','queued'},
                                speaking=self.speech_active(), overlap=envelope.overlap, enabled=True)):
                        return
                    ack_key = (epoch, -self.turn_sequence)
                    self.reply_input_ids[ack_key] = [u.input_id for u in envelope.current_utterances]
                    self.voice_timings[ack_key] = (monotonic(), {'route':'waiting_ack'})
                    await self._start_plan((ResponseItem('verbal','我再想一下。',(envelope.speaker.object_id,)),),
                                           envelope.speaker.object_id, epoch, ack_key)
                def schedule_reconsider():
                    task = asyncio.create_task(reconsider_feedback())
                    self.background_tasks.add(task)
                    task.add_done_callback(self.background_tasks.discard)
                loop.call_soon_threadsafe(schedule_reconsider)
        def review_ready(response, incoming):
            nonlocal envelope
            reviewed, envelope, speaker = self.apply_main_review(response, incoming, epoch)
            return reviewed, envelope, speaker
        def experience():
            from jshi.subject.cancellation import generation_scope
            with generation_scope(lambda: not self.closed and epoch == self.epoch and turn_key not in self.retired_turns):
                result = self.process.experience(self.subject_id, envelope.text, envelope=envelope,
                    objects={u.speaker.label:u.speaker.object_id for u in (*envelope.utterances, *envelope.current_utterances) if u.speaker.status in {"introduced", "recognized"}},
                    resolved_speaker=self.identities.candidate(envelope.speaker), on_voice_plan=plan_ready,
                    on_phase=phase_ready,
                    defer_write=True, code_restore=self.restore_codes,
                    preparation_context=preparation_context, light_finalize=prepared is not None,
                    action_allowed=lambda: not self.closed and epoch == self.epoch and turn_key not in self.retired_turns,
                    on_conversation_review=review_ready,
                    object_codes={oid: code for code, oid in self.code_owner.items()})
            return result, self.process.last_model_response, result.selected_tool_ids

        result, response, selected = await loop.run_in_executor(self.turn_executor, experience)
        metrics['recall_activity_id'] = result.activity.id
        if metrics.get('recall_delivery_status'):
            self.process.note_recall_delivery(result.activity.id, metrics['recall_delivery_status'])
        with stage(self.record, 'main_reply_cache', activity_id=result.activity.id,
                   input_ids=[u.input_id for u in envelope.current_utterances]):
            self.direct_replies.prime(self.subject_id, result, include_reply=False)
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
        with stage(self.record, 'main_debug_persist', activity_id=result.activity.id,
                   input_ids=[u.input_id for u in envelope.current_utterances]):
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
        return result.response_plan

    async def _start_reply(self, text: str, object_id: str, epoch: int) -> None:
        await self._start_plan((ResponseItem("verbal", text, (object_id,)),), object_id, epoch, epoch)

    async def _direct_reply(self, text, object_id, epoch, envelope, current, call):
        if ('shared_scene_revision' in call and
                tuple(call['shared_scene_revision']) != self._shared_scene_revision()):
            self.record('jev_reply_withheld', {'reason': '主认知认识更新已到达，旧短答不再交付'})
            return
        from jshi.core.contracts import utc_now
        from jshi.effectiveness.timings import append_timing
        from jshi.subject.process import ActivityTiming
        text = self.restore_codes(text)
        ready = time()*1000
        received = min((u.received_at_ms for u in current if u.received_at_ms is not None), default=None)
        elapsed = round(max(0, ready-received), 1) if received is not None else None
        activity_id = call.get('prompt_activity_id') or 'jev-'+current[0].input_id
        end = {'route':'self_reply', 'reply_ready_ms':elapsed, 'complete_ms':elapsed,
               'entry_total_ms':elapsed, 'jev_ms':call.get('ms'),
               'input_prepare_ms':call.get('input_prepare_ms'), 'main_process_ms':0,
               'main_cognition_ms':0, 'main_called':False, 'memory_written':False,
               'scope':'server_received_to_reply', 'background_write_included':False}
        timing = ActivityTiming(activity_id, utc_now().isoformat(), (), 0, end)
        append_timing(self.process._timings_path, subject_id=self.subject_id, **asdict(timing))
        metrics = {'reply_ready_at_ms':ready, 'received_to_reaction_ms':elapsed,
                   'current_jev_ms':call.get('ms'), 'jev_calls':[call], 'route':'self_reply'}
        self.voice_timings[activity_id] = (monotonic(), metrics)
        if len(self.voice_timings) > 32:
            self.voice_timings.pop(next(iter(self.voice_timings)))
        self.last_debug = {'activity_id':activity_id, 'input':envelope.to_dict(),
            'jev':call, 'timing':{**asdict(timing), 'voice':metrics},
            'response':{'route':'self_reply','reply':text}, 'tool':{'selected_tool_ids':[]}}
        self.debug_history.append(self.last_debug)
        self.reply_input_ids[activity_id] = [u.input_id for u in current]
        await self._start_plan((ResponseItem('verbal', text, (object_id,)),), object_id, epoch, activity_id)
        await self.emit('jev_reply', text=text, route='self_reply', main_called=False, memory_written=False)

    async def _release_deferred_entry(self) -> None:
        group = self.entry_scheduler.deferred_group
        self.entry_scheduler.deferred_group = None
        if group:
            self.judge_pending.appendleft(group)
            self.judge_ready.set()

    async def apply_schedule_effect(self, effect) -> None:
        if effect is None or effect.kind in {'none', 'ignored', 'observe', 'normal_entry', 'text',
                'hold_submission', 'diagnostic', 'stale', 'replace_entry', 'capped', 'ack', 'await_asr'}:
            return
        snap = self.delivery.snapshot()
        reply_id = snap.get('reply_id', '')
        if effect.kind == 'block_next':
            await self.emit('block_next', reply_id=reply_id, reason_id=effect.reason)
        elif effect.kind == 'pause':
            self.pending_voice = True
            if self.delivery.pause():
                await self.emit('pause', reply_id=reply_id, permit_seq=effect.seq, reason_id=effect.reason)
        elif effect.kind in {'clear_block', 'reapprove'}:
            await self.emit('clear_block', reply_id=reply_id, cleared=list(effect.cleared))
            if effect.resume:
                self.pending_voice = False
                self.delivery.resume()
                await self.emit('resume', reply_id=reply_id, cleared=list(effect.cleared))
                await self._next_plan()
        elif effect.kind == 'unclear':
            self.pending_voice = True
            if self.delivery.pause():
                await self.emit('pause', reply_id=reply_id, permit_seq=effect.seq, reason_id='unclear')
        elif effect.kind == 'invalidate_work':
            self._retire_answers('替换', reason='premise_changed')
            self._bump_work_epoch(effect)
            self.preparation.cancel()
            self.pending_plans.clear()
            if self.tts_task and not self.tts_task.done():
                self.tts_task.cancel()
            rid = self.delivery.stop('premise_changed')
            await self.emit('stop', reply_id=rid, reason_id='premise_changed')
        elif effect.kind == 'cancel':
            self._retire_answers('取消', reason=effect.reason or 'schedule_cancel')
            self._bump_work_epoch(effect)
            self.preparation.cancel()
            rid = self.delivery.stop(effect.reason or 'schedule_cancel')
            if self.tts_task and not self.tts_task.done():
                self.tts_task.cancel()
            self.pending_plans.clear()
            await self.emit('stop', reply_id=rid, permit_seq=effect.seq, reason_id=effect.reason or 'cancel')
            await self._persist_schedule(effect)

    def _bump_work_epoch(self, effect) -> None:
        self.epoch += 1
        if effect.covered:
            self.work_epoch_inputs[self.epoch] = set(effect.covered)
            while len(self.work_epoch_inputs) > 16:
                self.work_epoch_inputs.pop(next(iter(self.work_epoch_inputs)))

    def _epoch_current(self, epoch, input_ids) -> bool:
        """Old work invalidated because of these very inputs does not invalidate them."""
        if epoch == self.epoch:
            return True
        ids = set(input_ids)
        return bool(ids) and epoch < self.epoch and all(
            ids & self.work_epoch_inputs.get(e, set()) for e in range(epoch + 1, self.epoch + 1))

    async def _persist_schedule(self, effect) -> None:
        if not effect.persist:
            return
        try:
            await asyncio.to_thread(self.jev_scene_store.note_control, effect.matter_id, effect.persist[-1])
        except OSError:
            self.entry_scheduler.schedule.persist_failed(effect.matter_id)
            self.record('schedule_persist_failed', {'matter_id': effect.matter_id})

    async def _confirm_transient(self, token, matter_id=None) -> None:
        await asyncio.sleep(self.entry_scheduler.schedule.transient_confirm_s)
        effect = self.entry_scheduler.schedule.confirm_transient(token, matter_id)
        if effect.kind != 'none':
            await self.apply_schedule_effect(effect)

    async def _expire_asr_wait(self, token, matter_id=None) -> None:
        await asyncio.sleep(self.entry_scheduler.schedule.asr_wait_s)
        effect = self.entry_scheduler.schedule.expire_asr_wait(token, matter_id)
        if effect.kind != 'none':
            self.record('voice_asr_wait_expired', {'matter_id': effect.matter_id, 'cleared': list(effect.cleared),
                                                   'wait_s': self.entry_scheduler.schedule.asr_wait_s})
            await self.apply_schedule_effect(effect)

    async def _start_plan(self, items, object_id, epoch, turn_key) -> None:
        metrics = self.voice_timings.get(turn_key, (None, {}))[1]
        if metrics.get('route') == 'self_reply':
            call = next(iter(metrics.get('jev_calls', ())), {})
            if ('shared_scene_revision' in call and
                    tuple(call['shared_scene_revision']) != self._shared_scene_revision()):
                self.record('jev_reply_withheld', {'reason': '排队期间认识已更新，旧短答不再交付'})
                return
        if self.closed or epoch != self.epoch or turn_key in self.retired_turns:
            self.record('voice_answer_discarded', {'turn_key': turn_key,
                'input_ids': self.reply_input_ids.get(turn_key, []),
                'reason': self.retired_turns.get(turn_key, 'closed' if self.closed else 'epoch_changed')})
            return
        self.answer_status[turn_key] = 'ready'
        if self.entry_scheduler.schedule.blocks_start():
            matter = self.entry_scheduler.schedule.current()
            self.record('voice_answer_deferred', {'reason': 'schedule_block', 'turn_key': turn_key,
                'input_ids': self.reply_input_ids.get(turn_key, []),
                'pending_inputs': list(matter.pending) if matter else [],
                'unclear': bool(matter and matter.unclear),
                'cancelled': bool(matter and matter.cancelled),
                'asr_pending': bool(matter and matter.asr_pending),
                'reasons': list(matter.reasons) if matter else []})
            if len(self.pending_plans) == self.pending_plans.maxlen:
                await self.emit("notice", text="待播回应较多，暂不增加新语音。")
                return
            self.pending_plans.append((items, object_id, epoch, turn_key))
            return
        from jshi.voice.delivery import sentences
        key = self.session_id + ':' + str(turn_key)
        # Waiting acknowledgements are already scheduled by the host. Keep their wording
        # visible to main cognition without waiting for player playback receipts.
        hide = self.voice_timings.get(turn_key, (None, {}))[1].get('route') != 'waiting_ack'
        self.dialogue.plan(self.subject_id, key, self.session_id,
                           [segment for item in items for segment in sentences(item.text)],
                           hide_until_played=hide)
        if self.delivery.snapshot().get("state") in {"queued", "playing", "paused"}:
            self.record('voice_answer_deferred', {'reason': 'player_busy', 'turn_key': turn_key,
                'input_ids': self.reply_input_ids.get(turn_key, []),
                'playing_reply_id': self.delivery.snapshot().get('reply_id')})
            if len(self.pending_plans) == self.pending_plans.maxlen:
                await self.emit("notice", text="待播回应较多，暂不增加新语音。")
                return
            self.pending_plans.append((items, object_id, epoch, turn_key))
            return
        text = "\n".join(i.text for i in items)
        d = self.delivery.begin(text, object_id, items=items)
        schedule = self.entry_scheduler.schedule
        schedule.add_answer(d.reply_id, schedule.capture().input_version)
        self.dialogue.bind(key, d.reply_id)
        if self.voice_timings.get(turn_key, (None, {}))[1].get('route') in {'self_reply', 'waiting_ack'}:
            self.direct_reply_ids.add(d.reply_id)
        self.reply_records[turn_key] = d
        self.reply_epochs[d.reply_id] = turn_key
        # A long-running voice session should not retain every synthesized reply.
        if len(self.reply_records) > 32:
            for old in list(self.reply_records)[:-32]:
                old_reply = self.reply_records.pop(old)
                self.direct_reply_ids.discard(old_reply.reply_id)
                self.reply_epochs.pop(old_reply.reply_id, None)
                self.tool_handling.pop(old, None)
        if self.pending_voice:
            self.delivery.pause()
        reply_sent_at_ms = time()*1000
        reply_kind = ('waiting_ack' if self.voice_timings.get(turn_key, (None, {}))[1].get('route') == 'waiting_ack'
                      else 'answer')
        for iid in self.reply_input_ids.get(turn_key, ()):
            if reply_kind == 'waiting_ack':
                self.stamp_pipeline(iid, waiting_reply_sent_at_ms=reply_sent_at_ms)
            else:
                self.stamp_pipeline(iid, reply_sent_at_ms=reply_sent_at_ms)
        reply_items = []
        for item in items:
            row = asdict(item)
            row['target_labels'] = [self.reply_target_name(target) for target in item.target_ids]
            reply_items.append(row)
        await self.emit("reply", reply_id=d.reply_id, text=text, paused=self.pending_voice,
                        reply_kind=reply_kind, sent_at_ms=reply_sent_at_ms,
                        input_ids=list(self.reply_input_ids.get(turn_key, ())),
                        turn_key=list(turn_key),
                        generated_at_ms=self.voice_timings.get(turn_key, (None, {}))[1].get('reply_ready_at_ms'),
                        items=reply_items)
        await self.publish_pipeline(self.reply_input_ids.get(turn_key, ()))
        self.record("voice_delivery", self.delivery.snapshot())
        self.attention.engage(t for item in items for t in item.target_ids)
        self.tts_task = asyncio.create_task(self._synthesize(d))

    async def _next_plan(self):
        if self.entry_scheduler.schedule.blocks_start():
            return
        while self.pending_plans and self.delivery.snapshot().get("state") in {None, "completed", "failed", "stopped"}:
            await self._start_plan(*self.pending_plans.popleft())
            if self.entry_scheduler.schedule.blocks_start():
                break

    async def _synthesize(self, d) -> None:
        synthesis_started = monotonic()
        try:
            for segment, text in enumerate(d.texts):
                from jshi.voice.tts_retry import sentence_audio
                stream = sentence_audio(self.cloud, text,
                    is_current=lambda: self.delivery.accepts(d.reply_id) and not self.closed,
                    on_retry=lambda error: self.record('voice_tts_retry',
                        {'reply_id': d.reply_id, 'segment': segment, 'error_type': error, 'audio_sent': False}))
                try:
                    async for pcm in stream:
                        if not self.delivery.accepts(d.reply_id):
                            return
                        measured = self.voice_timings.get(self.reply_epochs.get(d.reply_id))
                        if measured and 'first_audio_ms' not in measured[1]:
                            measured[1]['first_audio_ms'] = round((monotonic() - measured[0]) * 1000)
                            measured[1]['synthesis_first_packet_ms'] = round((monotonic() - synthesis_started) * 1000)
                            self.record('voice_tts_first_packet', {
                                'reply_id':d.reply_id, 'segment':segment, 'at_ms':time()*1000,
                                'synthesis_first_packet_ms':measured[1]['synthesis_first_packet_ms'],
                                'reply_ready_at_ms':measured[1].get('reply_ready_at_ms'),
                                'transport':{k:v for k,v in getattr(getattr(self.cloud,'_tts',None),'last_metrics',{}).items() if k.endswith('_ms')}})
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
            self.answer_status[self.reply_epochs.get(d.reply_id)] = 'failed'
            self.record("voice_delivery", self.delivery.snapshot())
            if d.reply_id not in self.direct_reply_ids:
                self.process.note_scene_delivery(self.subject_id, f"failed:{d.reply_id}",
                    "（播放器交付失败）" + json.dumps(self.delivery.snapshot(), ensure_ascii=False)
                    + "；partial_text未确认整句播完，pending_text尚未播放。")
            await self.emit("stop", reply_id=d.reply_id)
            await self.emit("notice", text=f"语音合成失败，文字回应已保留：{exc}")
            await self._next_plan()

    async def acknowledge(self, payload: dict) -> None:
        if self.delivery.acknowledge(str(payload.get("reply_id", "")), int(payload.get("segment", -1)), payload.get("phase", "")):
            self.record('voice_playback_clock', {'reply_id':payload.get('reply_id'),
                'segment':payload.get('segment'), 'phase':payload.get('phase'),
                'browser_at_ms':payload.get('browser_at_ms'), 'received_at_ms':time()*1000,
                'audio_received_at_ms':payload.get('audio_received_at_ms'),
                'audio_wait_ms':payload.get('audio_wait_ms')})
            key = self.reply_epochs.get(str(payload.get('reply_id', '')))
            self.answer_status[key] = ('completed' if len(self.delivery.current.completed) == len(self.delivery.current.texts) else 'playing')
            if payload.get('phase') == 'completed':
                self.dialogue.played(str(payload['reply_id']), int(payload['segment']))
                self.entry_scheduler.schedule.ack(str(payload['reply_id']), int(payload['segment']),
                    final=int(payload['segment']) == len(self.delivery.current.texts)-1,
                    quick=str(payload['reply_id']) in self.direct_reply_ids)
            measured = self.voice_timings.get(self.reply_epochs.get(str(payload.get('reply_id',''))))
            if measured and payload.get('phase') == 'started' and 'playback_start_ms' not in measured[1]:
                measured[1]['playback_start_ms'] = round((monotonic() - measured[0]) * 1000)
            self.record("voice_delivery", self.delivery.snapshot())
            if str(payload.get('reply_id', '')) in self.direct_reply_ids:
                if payload.get('phase') == 'completed':
                    d = self.delivery.current
                    self.played_history.append({'who':'匠石', 'basis':'已播出',
                        'text':d.texts[int(payload['segment'])], 'targets':list(d.targets[int(payload['segment'])]),
                        'at':time()*1000})
                    if measured:
                        self.jev_scene_store.append(f"played:{d.reply_id}:{payload['segment']}",
                            {'p':'匠石', 'text':d.texts[int(payload['segment'])],
                             'basis':'等待接话已播出' if measured[1].get('route') == 'waiting_ack' else 'JEV回复已播出', 'at':time()*1000})
                        self.process.note_scene_delivery(self.subject_id,
                            f"played:{d.reply_id}:{payload['segment']}",
                            '已播｜匠石：' + d.texts[int(payload['segment'])])
                    await self._next_plan()
                return
            if payload.get("phase") == "completed":
                d = self.delivery.current
                key = self.reply_epochs.get(d.reply_id)
                recall_metrics = self.voice_timings.get(key, (None, {}))[1]
                status = 'played' if d.state == 'completed' else 'partially_played'
                recall_metrics['recall_delivery_status'] = status
                if recall_metrics.get('recall_activity_id'):
                    self.process.note_recall_delivery(recall_metrics['recall_activity_id'], status)
                targets = d.targets[int(payload['segment'])] if d.targets else (d.object_id,)
                self.process.note_scene_delivery(
                    self.subject_id, f"played:{d.reply_id}:{payload['segment']}",
                    "已播｜匠石：" + d.texts[int(payload['segment'])],
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
                        text_raw=d.texts[int(payload["segment"])],
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
        await self.preparation.close()
        await self.entry_scheduler.close()
        self.pending_environment = None
        if self.environment_task is not None:
            self.environment_task.cancel()
            await asyncio.gather(self.environment_task, return_exceptions=True)
        executor = getattr(self.process, 'reflection_executor', None)
        if executor and self._reflection_token:
            executor.detach(self._reflection_token)
        if self.background_tasks:
            progress('等待登记或诊断任务结束')
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        self.worker.cancel()
        for job in self.personal_prediction_jobs.values():
            job.cancel()
        await asyncio.gather(*self.personal_prediction_jobs.values(), return_exceptions=True)
        self.personal_prediction_jobs.clear()
        if self.parallel_identity is not None:
            await asyncio.to_thread(self.parallel_identity.close)
        self.input_records.clear()
        self.annotations.clear()
        self.worker.cancel()
        self.judge_worker.cancel()
        self.turn_worker.cancel()
        self.person_review_worker.cancel()
        await asyncio.gather(self.worker, self.judge_worker, self.turn_worker, self.person_review_worker, return_exceptions=True)
        if self.input_carry is not None:
            self.input_carry = None
            self.queue.task_done()
        self.person_review_executor.shutdown(wait=False, cancel_futures=True)
        self.audio_preparer.close()
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


def create_app(process, subject_id: str, config: VoiceConfig, jev=None, local=None, local_tts=None, online_speakers=None, *, speaker_models=None, trial_root=None, personal_speakers=None):
    from aiohttp import web, ClientSession, ClientTimeout, WSMsgType

    visual = getattr(process, 'vision', None)
    from jshi.voice.direct_reply import reply_state
    text_direct_replies = reply_state(process)
    from jshi.voice.dialogue import dialogue_state
    dialogue = dialogue_state(process)
    from jshi.voice.recent_audio import RecentAudio
    recent_audio = RecentAudio(process.repository.path.parent/'recent_audio'/
        hashlib.sha256(subject_id.encode()).hexdigest()[:16])
    dialogue.import_history(subject_id, process.repository.list_history(subject_id, HistoryKind.FACT, 2000))
    app = web.Application(client_max_size=max(8 * 1024 * 1024, visual.config.max_image_bytes if visual else 0))
    busy = False
    cleanup_stage = ""
    registry_busy = False
    text_write_task = None
    pending_text_write = None
    text_write_error = ''
    from jshi.voice.jev_scene import JEVSceneStore, JEVSceneWriter
    text_jev_store = JEVSceneStore(process.repository.path.parent / 'jev_scenes' /
        hashlib.sha256(subject_id.encode()).hexdigest()[:16] / 'text-entry.json')
    text_jev_task = None
    text_jev_error = ''
    text_jev_writer = None
    active_conversation = None
    model_notice_sink = None
    model_status_sink = None
    environment_pending = None
    if personal_speakers is None:
        try:
            from jshi.voice.personal import PersonalSpeakers
            personal_speakers = PersonalSpeakers(process.repository.path.parent / 'personal_speakers' /
                hashlib.sha256(subject_id.encode()).hexdigest()[:16], process.profiles)
        except ImportError:
            logging.getLogger(__name__).info('Personal classifiers unavailable: install voice-personal extra')

    async def close_personal(app):
        if personal_speakers is not None:
            await asyncio.to_thread(personal_speakers.close)
    app.on_cleanup.append(close_personal)
    async def close_local_pipeline(app):
        if local is not None and hasattr(local, 'close'):
            await asyncio.to_thread(local.close)
    app.on_cleanup.append(close_local_pipeline)

    async def deliver_environment(environment, decision):
        nonlocal registry_busy, environment_pending
        if environment.get('reason') in {'local_detection', 'sample'}:
            return  # Reused descriptions are display state, not a new cognitive stimulus.
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
                if envelope.jev_calls[-1].get('route') not in {'background', 'wait'}:
                    await asyncio.to_thread(process.experience, subject_id, envelope.text, envelope=envelope,
                        processing_modes=('environment',), defer_write=True)
                    schedule_text_scene()
            finally:
                registry_busy = False

    async def judge_standalone(envelope, *, waiting_allowed=False):
        from jshi.voice.jev_scene import JEVSceneStore
        import hashlib
        scene_store = text_jev_store
        shared = process.pending_scene.shared_events(subject_id)
        shared_revision = tuple(row['id'] for row in shared)
        scene_store.sync_shared_events([row for row in shared if row['at'] >= time()*1000 - 300000])
        saved = scene_store.snapshot()
        codes, labels = dict(saved['codes']), dict(saved['labels'])
        selected_code = next((p for p, oid in codes.items() if oid == envelope.speaker.object_id), '')
        if not selected_code and envelope.speaker.method == 'user_selected':
            selected_code = f'P{max([int(p[1:]) for p in codes if p[1:].isdigit()] or [0])+1}'
            codes[selected_code] = envelope.speaker.object_id
            labels[envelope.speaker.object_id] = envelope.speaker.label
        material = scene_store.material_view(time()*1000)
        scene, unwritten = material['scene'], material['events']
        prepare_started = monotonic()
        envelope = await asyncio.to_thread(pack_input, envelope, visual, subject_id)
        prepared = monotonic()
        judge = jev or VoiceJEV()
        def capture(request):
            process._record_step_input(request, judge.model, subject_id=subject_id, activity_id='jev-'+envelope.input_id)
        jev_started = monotonic()
        from jshi.voice.wait_ack import wait_timing, model_key
        from jshi.voice.jev_material import task_context
        tasks = await asyncio.to_thread(task_context, process, subject_id, [envelope.speaker])
        generation = material['generation']
        from jshi.voice.jev_decision import entry_bridge
        from jshi.decision import decision_service
        from copy import copy
        original_judge = judge
        judge = copy(judge)
        judge._cooldown_owner = original_judge
        from jshi.core.person_names import known_names
        result = await asyncio.to_thread(judge.decide_envelope, subject_id, envelope,
            {'_decision_entry': entry_bridge(decision_service(process)),
             'state':'completed', 'jev_scene':scene, 'unwritten':unwritten, 'now':time()*1000,
             'conversation':dialogue.recent(subject_id, exclude={envelope.input_id}),
             'task_context':tasks, 'selected_person_code':selected_code or 'P1',
             'named_people': known_names(codes, process.profiles),
             'people':{p:labels.get(oid,p) for p,oid in codes.items()}, 'jev_material_scope':{k:material[k] for k in ('version','generation','archived_unread_count')},
             'waiting':wait_timing(process).guidance(model_key(process), allowed=waiting_allowed, mode='text'),
             'short_chat':{**text_direct_replies.context(subject_id, [envelope.speaker.object_id], allow_general=judge.mode != "legacy"),
                           'person_code':selected_code or 'unknown'}}, vision=visual, on_request=capture,
            is_current=lambda: scene_store.state['generation'] == generation
                and process.pending_scene.shared_revision(subject_id) == shared_revision)
        jev_finished = monotonic()
        record = {'main_prompt_hint':result.main_prompt_hint, 'judged':result.judged,
                  'model_error':result.model_error, 'items':[asdict(i) for i in result.items],
                  'input_ids':[envelope.input_id], 'entry_support':list(result.entry_support),
                  'direct_reply':result.direct_reply,
                  'waiting_reply':result.waiting_reply,
                  'front_choice':result.front_choice, 'route':result.route,
                  'decision_reason':result.decision_reason, 'decision_refs':list(result.decision_refs),
                  'control':result.control, 'utterance_complete':result.utterance_complete,
                  'entry_route':result.entry_route,'process_refs':list(result.process_refs),'pending_refs':list(result.pending_refs),
                  'interaction':result.interaction,'stages':list(result.stage_records),'silent_operation':result.silent_operation,
                  'mode':getattr(judge,'mode','legacy'),
                  'ms':round((jev_finished-jev_started)*1000, 1),
                  'input_prepare_ms':round((prepared-prepare_started)*1000, 1),
                  'prompt_activity_id':'jev-'+envelope.input_id}
        failures = [s for s in result.stage_records if s.get('status') == 'failed']
        failure = failures[-1] if failures else {}
        record['failure_kind'] = failure.get('error_kind', 'unclassified' if result.model_error else '')
        record['failure_stage'] = failure.get('stage', '')
        # An explicitly attributed text is a direct request even if the gate fails.
        # Preserve a valid stop and reject stale evidence rather than overriding it.
        fallback = bool(result.model_error and envelope.speaker.method == 'user_selected'
                        and result.control != 'stop' and record['failure_kind'] != 'stale')
        record['fallback_to_main'] = fallback
        record['gate_route'] = result.route
        if fallback:
            record.update(route='main', direct_reply='', waiting_reply='',
                          main_prompt_hint=None, control='keep')
        process.repository.add_history(HistoryRecord(subject_id=subject_id, kind=HistoryKind.FACT,
            event_type='text_jev_diagnostics', content={'input': envelope.text,
                'input_id': envelope.input_id, **record}, source_ids=()))
        if result.model_error and not fallback:
            # Failure is never reported as a successful background/control decision.
            raise RuntimeError('入口判断失败（'+record['failure_kind']+'）：'+result.model_error)
        from jshi.recognition.acquaintance import acquaintance_store
        acquaintances=acquaintance_store(process)
        acquaintances.observe_text(subject_id,envelope.speaker.object_id,envelope.input_id,envelope.text,session=envelope.session_id)
        dialogue.input(subject_id, envelope.input_id, envelope.text, envelope.speaker.label)
        judgment = result.items[0] if result.items else None
        scene_store.append(envelope.input_id, {'text':envelope.text, 'at':time()*1000,
            'p': selected_code or 'unknown', 'source':envelope.source, 'basis':'统一输入信封',
            'entry_status':'completed' if result.judged else 'failed',
            'entry_interaction':result.interaction,
            'judgment': {'person':judgment.speaker_pick,'certainty':judgment.speaker_level,
                         'why':judgment.speaker_reason} if judgment else {}}, codes=codes, labels=labels)
        schedule_text_jev()
        utterance = SceneUtterance(envelope.text, envelope.speaker, 0, 0, input_id=envelope.input_id,
                                    received_at_ms=time()*1000, identity_note='统一信封来源：'+envelope.source)
        entry = dict(envelope.entry_timing)
        entry.update(input_prepare_ms=record['input_prepare_ms'], jev_ms=record['ms'],
                     jev_fallback=fallback, jev_failure_kind=record['failure_kind'])
        return replace(envelope, jev_calls=(record,), current_utterances=(utterance,), entry_timing=entry,
                       delivery_context=dialogue.note(subject_id, exclude={envelope.input_id} | process.pending_scene.input_ids(subject_id)))

    async def flush_text_jev():
        nonlocal text_jev_error, text_jev_writer
        model = getattr(process, 'person_review_model', None) or getattr(jev, 'model', None)
        if text_jev_writer is None:
            text_jev_writer = JEVSceneWriter(model)
        text_jev_writer.model = model
        writer = text_jev_writer
        for _ in range(4):
            text_jev_store.sync_shared_events(process.pending_scene.shared_events(subject_id,
                since_ms=time()*1000 - 300000))
            snapshot = text_jev_store.snapshot()
            if not snapshot['events']:
                return
            prompt_activity_id = 'jev-scene-text-'+uuid4().hex
            try:
                main_reference = await asyncio.to_thread(process.person_review_scene, subject_id)
                main_scene = main_reference.get('committed_scene', '')
                from jshi.core.person_names import known_names
                snapshot['named_people'] = known_names(snapshot['codes'], process.profiles)
                scene, _ = await asyncio.to_thread(writer.run, subject_id, snapshot, main_scene,
                    on_request=lambda request: process._record_step_input(request, model,
                        subject_id=subject_id, activity_id=prompt_activity_id))
                text_jev_store.sync_shared_events(process.pending_scene.shared_events(subject_id,
                    since_ms=time()*1000 - 300000))
                accepted = await asyncio.to_thread(text_jev_store.commit, snapshot, scene)
                text_jev_error = '' if accepted else '片场结果过期，事件保留'
            except Exception as exc:
                text_jev_error = str(exc)
                from jshi.voice.jev_errors import failure_details
                failure = snapshot.get('failure') or failure_details(exc, 'output_schema' if isinstance(exc, ValueError) else 'internal')
                process.repository.add_history(HistoryRecord(subject_id=subject_id, kind=HistoryKind.FACT,
                    event_type='text_jev_scene_failure', content={**failure,
                        'prompt_activity_id': prompt_activity_id,
                        'model_raw_chars': len(snapshot.get('model_raw', '')),
                        'model_raw': snapshot.get('model_raw', '')[:16000],
                        'snapshot_version': snapshot['version'], 'generation': snapshot['generation']}, source_ids=()))
                logging.getLogger(__name__).exception('Text JEV2 scene update failed; events retained')
                return

    def schedule_text_jev():
        nonlocal text_jev_task
        model = getattr(process, 'person_review_model', None) or getattr(jev, 'model', None)
        if model is not None and getattr(jev, 'mode', 'split') != 'legacy' and (text_jev_task is None or text_jev_task.done()):
            text_jev_task = asyncio.create_task(flush_text_jev())

    def record_text_jev_delivery(result, incoming):
        from jshi.recognition.acquaintance import acquaintance_store
        acquaintance_store(process).reply_delivered(subject_id,incoming.speaker.object_id,
            'text-question:'+incoming.input_id,result.response_plan.verbal_text(),medium='text')

        if getattr(jev, 'mode', 'split') == 'legacy' or not result.response_plan.verbal_text().strip():
            return
        text_jev_store.append('text-response:'+incoming.input_id,
            {'p':'匠石', 'text':result.response_plan.verbal_text(), 'at':time()*1000,
             'basis':'文字回应已安排发送，未确认页面显示或阅读'})
        schedule_text_jev()

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
        if text_jev_task is not None:
            await asyncio.shield(text_jev_task)
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

    async def recent_audio_page(request):
        return web.FileResponse(Path(__file__).with_name('recent_audio.html'))

    async def recent_audio_index(request):
        return web.json_response({'hours':recent_audio.hours,'max_bytes':recent_audio.max_bytes,
            'inputs':await asyncio.to_thread(recent_audio.recent),
            'recordings':await asyncio.to_thread(recent_audio.continuous)},headers={'Cache-Control':'no-store'})

    async def recent_audio_stream(request):
        try:start=int(request.match_info['start'])
        except ValueError:raise web.HTTPBadRequest(text='音频位置无效')
        body=await asyncio.to_thread(recent_audio.stream_wav,request.match_info['capture'],start)
        if body is None:raise web.HTTPNotFound(text='连续录音不存在或已清理')
        return web.Response(body=body,content_type='audio/wav',headers={'Cache-Control':'no-store'})

    async def recent_audio_item(request):
        iid=request.match_info['iid']
        if request.path.endswith('.wav'):
            try:
                padding=int(request.query.get('padding_ms','0'))
            except ValueError:
                raise web.HTTPBadRequest(text='音频边界参数无效')
            if not 0 <= padding <= 2000:
                raise web.HTTPBadRequest(text='前后音频最多各两秒')
            body=await asyncio.to_thread(recent_audio.wav,iid,padding_ms=padding)
            if body is None:raise web.HTTPNotFound(text='录音尚未保存或已超出保留范围')
            return web.Response(body=body,content_type='audio/wav',headers={'Cache-Control':'no-store'})
        value=await asyncio.to_thread(recent_audio.get,iid)
        if value is None:raise web.HTTPNotFound(text='录音记录不存在或已过期')
        return web.json_response(value,headers={'Cache-Control':'no-store'})

    async def voice_context_settings(request):
        if request.method == 'POST':
            if request.headers.get('Origin', '') != f'http://{request.host}':
                raise web.HTTPForbidden(text='上下文设置需要同源请求')
            try:
                payload = await request.json()
                await asyncio.to_thread(dialogue.configure, payload['include_unplayed'])
            except (ValueError, TypeError, KeyError) as exc:
                raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({'include_unplayed':dialogue.include_unplayed}, headers={'Cache-Control':'no-store'})

    async def worklet(request):
        return web.FileResponse(Path(__file__).with_name("voice_capture.js"))

    async def vision_chat_script(request):
        return web.FileResponse(Path(__file__).with_name('vision_chat.js'))

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
            if personal_speakers is not None:
                recording = 'enrollment-' + hashlib.sha256(b''.join(pcm)).hexdigest()[:32]
                for clip in pcm:
                    personal_speakers.submit_capture(clip, object_id, recording,
                        hashlib.sha256(clip).hexdigest(), speakers, source='manual')
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
        from jshi.voice.segmentation import segment_settings
        return web.json_response({"busy": busy, "cleanup_stage": cleanup_stage, "asr": getattr(local, 'backend', 'local') if local is not None else config.asr_backend,
            'text_scene_writing': text_write_task is not None and not text_write_task.done(),
            'text_scene_write_error': text_write_error,
            'text_jev_writing': text_jev_task is not None and not text_jev_task.done(),
            'text_jev_error': text_jev_error,
            'asr_backends': {name: asdict(spec) for name, spec in ASR_BACKENDS.items()},
            'tts': '本地固定音色' if local_tts else ('火山在线播报 · ' + config.template.speaker),
            'tts_backend': 'local' if local_tts else 'online',
            'tts_backends': {'local': local_tts is not None, 'online': bool(config.api_key)},
            'speech_policies': {'segment':config.segment_strategy,
                'silence_ms':segment_settings(config).silence_ms,
                'max_window_ms':round(config.max_window_s*1000), 'turn':config.turn_strategy,
                'turn_threshold':config.turn_threshold,
                'semantic':config.semantic_turn_strategy,
                'semantic_threshold':config.semantic_turn_threshold,
                'semantic_timeout_ms':round(config.semantic_turn_timeout_s*1000),
                'scope':'qwen_local_pipeline'},
            'speaker_models': list(speaker_models or {}),
            'speaker_model_default': os.getenv('JSHI_VOICE_SPEAKER_MODEL', 'cam++'),
            "local_available": local is not None and getattr(local, 'backend', 'local') == 'local',
            "qwen_available": local is not None and getattr(local, 'backend', '') == 'qwen', "online_available": bool(config.api_key),
            "deepgram_available": bool(config.deepgram_api_key), "bailian_available": bool(config.bailian_asr_api_key), "vision_available": visual is not None, "voice_active": active_conversation is not None},
            headers={"Cache-Control": "no-store"})

    async def person_links(request):
        from jshi.recognition.acquaintance import acquaintance_store
        from jshi.recognition.link_updates import apply_link
        store=acquaintance_store(process)
        if request.method=='POST':
            if request.headers.get('Origin','')!=f'http://{request.host}':
                raise web.HTTPForbidden(text='人物更正需要同源请求')
            payload=await request.json()
            key=str(payload.get('link_id',''))
            if key not in {r['id'] for r in store.links(subject_id,active_only=False)}:
                raise web.HTTPBadRequest(text='找不到本主体的关联记录')
            if payload.get('action')=='revoke':
                if active_conversation is not None:
                    active_conversation.revoke_person_link(key)
                else:
                    store.revoke(key)
                    await asyncio.to_thread(apply_link,process,key,personal=personal_speakers)
            elif payload.get('action')=='retry':
                await asyncio.to_thread(apply_link,process,key,personal=personal_speakers,
                    live_scene=active_conversation.jev_scene_store if active_conversation else None)
            else: raise web.HTTPBadRequest(text='无效操作')
        rows=[]
        for row in store.links(subject_id,active_only=False)[-50:]:
            source=process.profiles.get(row['source']);target=process.profiles.get(row['target'])
            rows.append({'id':row['id'],'source':source.label if source else '临时人物',
                'target':target.label if target else '已确认人物','inputs':len(row['inputs']),
                'active':bool(row['active']),'jobs':store.jobs(row['id'])})
        return web.json_response({'links':list(reversed(rows))},headers={'Cache-Control':'no-store'})

    async def resume_person_links(app):
        from jshi.recognition.link_updates import retry_links
        await asyncio.to_thread(retry_links,process,subject_id,personal=personal_speakers)
    app.on_startup.append(resume_person_links)

    async def text_people(request):
        return web.json_response({'people': [{'object_id': p.object_id, 'label': p.label, 'source': p.source}
            for p in process.profiles.list() if p.status != 'rejected' and p.source != 'voice_scene']},
            headers={'Cache-Control': 'no-store'})

    async def text_input(request):
        nonlocal registry_busy, model_notice_sink, model_status_sink
        received_mono, received_at_ms = monotonic(), time()*1000
        if request.headers.get('Origin', '') != f'http://{request.host}':
            raise web.HTTPForbidden(text='文字输入需要同源请求')
        if busy or registry_busy:
            raise web.HTTPConflict(text='正在处理其他会话。语音连接时请在当前页面发送文字；或结束语音后再试。')
        try:
            payload = await request.json()
            text = payload.get('text', '').strip()
            from jshi.simplified import simplify_text
            simplified = simplify_text(text)
            if simplified != text:
                process.repository.add_history(HistoryRecord(subject_id=subject_id, kind=HistoryKind.FACT,
                    event_type='original_text_evidence', content={'source':'web_text','text':text}, source_ids=()))
                text = simplified
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
            if profile.source=='voice_anonymous':
                from jshi.voice.jev import explicit_name
                from jshi.recognition.acquaintance import acquaintance_store
                state=acquaintance_store(process)
                from jshi.voice.identity import typed_name_answer
                name=explicit_name(text) or typed_name_answer(process,subject_id,evidence,text)
                if name:
                    from jshi.voice.identity import save_reported_name
                    save_reported_name(process.profiles,state,subject_id,profile.object_id,name,
                        event_id='text-name:'+uuid4().hex,basis='text_self_report',original_text=text)
                saved_name=state.context(subject_id,profile.object_id)['reported_name']
                if saved_name: evidence=replace(evidence,label=saved_name)

            envelope = InputEnvelope(session_id='web-text', source='web_text',
                parts=(InputPart('text', text),), speaker=evidence,
                entry_timing={'received_mono':received_mono, 'received_at_ms':received_at_ms,
                              'source':'web_text'})
            streaming = 'application/x-ndjson' in request.headers.get('Accept', '')
            events = asyncio.Queue()
            loop = asyncio.get_running_loop()
            model_status_sink = lambda event: loop.call_soon_threadsafe(events.put_nowait, dict(event, type='model_status'))
            model_notice_sink = lambda text: loop.call_soon_threadsafe(events.put_nowait, {'type':'model_notice','text':text})
            main_started = None
            main_measured = False
            main_level = 1
            def measure_main_wait():
                nonlocal main_measured
                if main_started is not None and not main_measured:
                    main_measured = True
                    from jshi.voice.wait_ack import wait_timing, model_key
                    try:
                        wait_timing(process).observe(model_key(process), main_level, monotonic()-main_started)
                    except OSError:
                        logging.getLogger(__name__).exception('Text waiting latency statistics could not be saved')
            def plan_ready(plan):
                measure_main_wait()
                loop.call_soon_threadsafe(events.put_nowait, {'type': 'reply',
                    'reply': plan.verbal_text(), 'mode': plan.mode, 'label': profile.label})
            def phase_ready(stage):
                if streaming:
                    loop.call_soon_threadsafe(events.put_nowait, {'type':'flow_phase', 'stage':stage})
            response = None
            if streaming:
                response = web.StreamResponse(headers={'Content-Type': 'application/x-ndjson; charset=utf-8',
                    'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
                await response.prepare(request)
                await response.write((json.dumps({'type':'accepted','label':profile.label},ensure_ascii=False)+'\n').encode())
            async def run_turn():
                nonlocal main_started, main_level
                main_called = False
                try:
                    phase_ready('jev')
                    judged_envelope = await judge_standalone(envelope,
                        waiting_allowed=streaming)
                    entry = dict(judged_envelope.entry_timing)
                    entry['entry_total_ms'] = round((monotonic()-received_mono)*1000, 1)
                    judged_envelope = replace(judged_envelope, entry_timing=entry)
                    control = judged_envelope.jev_calls[-1].get('control')
                    call = judged_envelope.jev_calls[-1]
                    if call.get('fallback_to_main') and streaming:
                        events.put_nowait({'type':'jev_fallback', 'failure_kind':call['failure_kind'],
                                           'failure_stage':call['failure_stage']})
                    if (judged_envelope.jev_calls[-1].get('route') in {'background', 'wait'}
                            or (control == 'stop' and not judged_envelope.jev_calls[-1].get('silent_operation'))
                            or (control == 'keep' and judged_envelope.jev_calls[-1].get('route') not in {'main', 'self_reply'})):
                        from types import SimpleNamespace
                        from jshi.subject.process import ActivityTiming
                        from jshi.core.contracts import utc_now
                        elapsed = round((monotonic()-received_mono)*1000, 1)
                        timing = ActivityTiming('jev-'+envelope.input_id, utc_now().isoformat(), (), 0,
                            {'route':'control','control':control,'reply_ready_ms':elapsed,'complete_ms':elapsed,
                             'main_called':False,'memory_written':False})
                        process.repository.add_history(HistoryRecord(subject_id=subject_id, kind=HistoryKind.FACT,
                            event_type='jev_control',content={'input':text,'control':control},source_ids=()))
                        return SimpleNamespace(response_plan=ResponsePlan('ignore'),action_text='',timing=timing,
                                               activity_id=timing.activity_id,route='control')
                    direct = judged_envelope.jev_calls[-1].get('direct_reply', '')
                    if direct and text_direct_replies.accept(subject_id, profile.object_id, text, direct, allow_general=judged_envelope.jev_calls[-1].get("mode", "legacy") != "legacy"):
                        from types import SimpleNamespace
                        from jshi.subject.process import ActivityTiming
                        plan = ResponsePlan('respond', items=(ResponseItem('verbal', direct, (profile.object_id,)),))
                        dialogue.plan(subject_id, 'text:'+envelope.input_id, 'web-text', [direct], shown=True)
                        process.repository.add_history(HistoryRecord(subject_id=subject_id, kind=HistoryKind.FACT,
                            event_type='jev_direct_reply', content={'input':text, 'reply':direct, 'delivery':'text_sent'}, source_ids=()))
                        plan_ready(plan) if streaming else None
                        elapsed = round((monotonic()-received_mono)*1000, 1)
                        from jshi.core.contracts import utc_now
                        timing = ActivityTiming('jev-'+envelope.input_id, utc_now().isoformat(), (), 0,
                            {'route':'self_reply', 'reply_ready_ms':elapsed, 'complete_ms':elapsed,
                             'entry_total_ms':elapsed, 'input_prepare_ms':entry['input_prepare_ms'],
                             'jev_ms':entry['jev_ms'], 'main_process_ms':0, 'main_cognition_ms':0,
                             'main_called':False, 'memory_written':False,
                             'scope':'server_received_to_reply', 'background_write_included':False})
                        from jshi.effectiveness.timings import append_timing
                        append_timing(process._timings_path, subject_id=subject_id, **asdict(timing))
                        return SimpleNamespace(response_plan=plan, action_text=direct, timing=timing,
                                               activity_id=timing.activity_id, route='self_reply')
                    main_called = True
                    call = judged_envelope.jev_calls[-1]
                    main_level = (call.get('main_prompt_hint') or {}).get('level', 1)
                    brief = call.get('waiting_reply', '')
                    from jshi.voice.wait_ack import wait_timing, ack_context
                    if brief and streaming and wait_timing(process).claim_ack():
                        events.put_nowait({'type':'waiting_ack', 'text':brief, 'label':profile.label})
                        # Sent to the stream, not a claim that it was heard or read.
                        judged_envelope = replace(judged_envelope,
                            delivery_context='\n'.join(filter(None,
                                (judged_envelope.delivery_context, ack_context(brief, channel='text')))))
                    main_started = monotonic()
                    result = await asyncio.to_thread(process.experience, subject_id, text, envelope=judged_envelope,
                        objects={profile.label:profile.object_id}, on_voice_plan=plan_ready if streaming else None,
                        on_phase=phase_ready,
                        defer_write=True)
                    measure_main_wait()
                    text_direct_replies.prime(subject_id, result)
                    return result
                finally:
                    if main_called:
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
                    record_text_jev_delivery(result, envelope)
                    event = {'type':'complete','reply':result.response_plan.verbal_text(),
                        'actions':[i.text for i in result.response_plan.items if i.channel=='embodied' and i.text.strip() and i.text.strip()!='无动作'],
                        'mode':result.response_plan.mode,'label':profile.label,
                        'scene_write':'none' if getattr(result,'route','main') in {'self_reply','control'} else 'background',
                        'route':getattr(result,'route','main'),
                        'jev_fallback':bool(result.timing and result.timing.end_to_end.get('jev_fallback')),
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
            record_text_jev_delivery(result, envelope)
            reply = result.action_text or ''
            return web.json_response({'reply': reply, 'mode': result.response_plan.mode,
                'scene_write': 'none' if getattr(result,'route','main') in {'self_reply','control'} else 'background',
                'route':getattr(result,'route','main'),
                'jev_fallback':bool(result.timing and result.timing.end_to_end.get('jev_fallback')),
                'timing': asdict(result.timing) if result.timing else None,
                'label': profile.label, 'activity_id': str(getattr(result, 'activity_id', ''))},
                headers={'Cache-Control': 'no-store'})
        finally:
            model_notice_sink = None
            model_status_sink = None
            registry_busy = False

    async def connection(request):
        nonlocal busy, cleanup_stage, active_conversation
        origin = request.headers.get("Origin", "")
        if origin != f"http://{request.host}":
            raise web.HTTPForbidden(text="voice connection requires same origin")
        if busy or registry_busy:
            raise web.HTTPConflict(text="已有一个语音会话，请先结束它。")
        backend = request.query.get("asr", getattr(local, 'backend', 'local') if local is not None else config.asr_backend)
        if backend not in ASR_BACKENDS:
            raise web.HTTPBadRequest(text="未知语音识别后端")
        tts_backend = request.query.get('tts') or ('local' if local_tts else 'online')
        if tts_backend not in {'local', 'online'}:
            raise web.HTTPBadRequest(text='未知语音播报后端')
        if request.query.get('tts') == 'local' and local_tts is None:
            raise web.HTTPBadRequest(text='本地播报尚未加载')
        if request.query.get('tts') == 'online' and not config.api_key:
            raise web.HTTPBadRequest(text='火山播报缺少语音密钥')
        use_local = local if local is not None and backend == getattr(local, 'backend', 'local') else None
        if backend in {'local', 'qwen'} and use_local is None:
            raise web.HTTPBadRequest(text=f"识别未加载，请用 --asr {backend} 启动")
        if backend == "online" and not config.api_key:
            raise web.HTTPBadRequest(text="线上识别缺少 JSHI_VOICE_API_KEY，配置后重启服务")
        if backend == 'deepgram' and not config.deepgram_api_key:
            raise web.HTTPBadRequest(text='Deepgram识别缺少JSHI_DEEPGRAM_API_KEY，配置后重启服务')
        if backend == 'bailian' and not config.bailian_asr_api_key:
            raise web.HTTPBadRequest(text='百炼识别缺少JSHI_BAILIAN_ASR_API_KEY或DASHSCOPE_API_KEY，配置后重启服务')
        if backend == 'bailian' and config.identity_mode == 'voiceprint':
            raise web.HTTPBadRequest(text='百炼识别请使用diarization模式；云端voiceprint仅用于火山')
        if backend == 'deepgram' and config.identity_mode == 'voiceprint':
            raise web.HTTPBadRequest(text='Deepgram请使用diarization模式；云端voiceprint仅用于火山')
        if backend == 'qwen' and config.identity_mode == 'voiceprint':
            raise web.HTTPBadRequest(text='Qwen请使用diarization模式；云端voiceprint仅用于火山')
        if config.separation_backend != 'off' and backend != 'qwen' and not config.api_key:
            raise web.HTTPBadRequest(text='当前后端分离音轨转写需要JSHI_VOICE_API_KEY')
        busy = True
        cleanup_stage = ""
        ws = web.WebSocketResponse(max_msg_size=128 * 1024, heartbeat=20)
        conversation = None
        receiver = None
        loop_watch = None
        asr = None
        local_executor = None
        local_pump = None
        online_turn = None
        online_turn_error = ''
        cloud_alignment = None
        audio_timeline = None
        recovery_intake = None
        speaker_split_error = ''
        cloud = None
        from jshi.voice.capture import PcmCapture
        capture_source = PcmCapture()
        audio = SessionAudio(archive=recent_audio)
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
                from jshi.voice.sources import SpeakerAudioPreparer, PyannoteSeparator
                preparer = SpeakerAudioPreparer(
                    PyannoteSeparator(device=config.separation_device) if config.separation_backend == 'pyannote' else None,
                    (use_local.refiner.transcribe_source if backend == 'qwen' else cloud.transcribe_source) if config.separation_backend == 'pyannote' else None,
                    timeout_s=config.separation_timeout_s,
                    transcribe_many=use_local.refiner.transcribe_sources if backend == 'qwen' and config.separation_backend == 'pyannote' else None)
                conversation = VoiceConversation(process, subject_id, local_tts if tts_backend == 'local' else cloud, ws.send_json, jev=jev,
                    timeout_s=config.jev_timeout_s, local_speakers=speakers, input_pause_s=config.input_pause_s, sample_source=audio,
                    candidate_timeout_s=config.candidate_timeout_s, input_max_batch_s=config.input_max_batch_s,
                    input_batch_chars=config.input_batch_chars,
                    environment_idle_s=config.environment_idle_s,
                    personal_speakers=personal_speakers, audio_preparer=preparer,
                    utterance_material=config.utterance_material,
                    session_id=request.query.get("session") or None)
                active_conversation = conversation
                if use_local is not None:
                    use_local.reset_session()
                    conversation.speech_active = getattr(getattr(use_local, 'detector', None), 'speech_active', lambda: False)
                    local_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jshi-local-asr")
                    from jshi.voice.local_input import LocalAudioPump
                    async def local_error(exc):
                        if not ws.closed:
                            await conversation.emit('notice', text=f'本地识别失败：{exc}')
                            await ws.close()
                    local_pump = LocalAudioPump(use_local, local_executor, conversation.accept, local_error,
                        accept_batch=conversation.accept_batch, on_timings=conversation.note_asr_timings,
                        on_turn_state=conversation.turn_analysis,
                        on_provisional=conversation.preparation.preview)
                else:
                    from jshi.voice.streaming_asr import open_stream
                    if speakers is not None:
                        local_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jshi-cloud-identity")
                    if backend in {'bailian', 'online'}:
                        from jshi.voice.cloud_alignment import CloudSpeakerAlignment
                        try:
                            from jshi.voice.runtime import build_cloud_analyzer
                            model_root = Path(os.getenv('JSHI_VOICE_LOCAL_DIR', str(process.repository.path.parent/'voice_models')))
                            acoustic_analyzer = await asyncio.to_thread(build_cloud_analyzer, model_root, None,
                                selected_model or os.getenv('JSHI_VOICE_SPEAKER_MODEL', 'cam++'))
                            from jshi.voice.segmentation_cache import SegmentationCache
                            from jshi.voice.analysis import SpeakerAnalyzer, ParallelIdentity
                            shared_segments = SegmentationCache(audio, acoustic_analyzer, conversation.record)
                            identity_analyzer = SpeakerAnalyzer(None, speakers)
                            conversation.parallel_identity = ParallelIdentity(speakers, conversation.personal_speakers,
                                conversation.record, budget_s=config.candidate_timeout_s, segments=shared_segments)
                            identity_analyzer.parallel_identity = conversation.parallel_identity
                            shared_segments.identity = conversation.parallel_identity
                            cloud_alignment = CloudSpeakerAlignment(audio, identity_analyzer,
                                                                   segments=shared_segments)
                            from jshi.voice.audio_timeline import AudioTimeline
                            # 06 and 04 share one acoustic model and its cached windows.
                            # With recovery disabled, 04 still precomputes every 6 s.
                            audio_timeline = AudioTimeline(audio, shared_segments, conversation.record,
                                step_ms=6000 if config.continuous_analysis == 'off' else 1000)
                            if local_executor is None:
                                local_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jshi-cloud-speakers')
                        except Exception as exc:
                            speaker_split_error = '本地多人分段不可用：' + type(exc).__name__
                    if backend == 'online' and config.turn_strategy != 'eager':
                        from jshi.voice.online_turn import build_online_turn_gate
                        model_root = Path(os.getenv('JSHI_VOICE_LOCAL_DIR', str(process.repository.path.parent/'voice_models')))
                        try:
                            from jshi.voice.turns import build_turn_policy
                            policy = await asyncio.to_thread(build_turn_policy, config, model_root)
                            online_turn = build_online_turn_gate(config, audio, conversation.accept, model_root,
                                policy=policy, report=conversation.record,
                                urgent=lambda: conversation.turn_busy or conversation.judging or conversation.delivery.snapshot().get('state') in
                                    {'playing','thinking','paused','queued'})
                            conversation.speech_active = online_turn.speech_active
                        except Exception as exc:
                            logging.getLogger(__name__).exception('Online acoustic completion unavailable')
                            online_turn_error = '本地声音整句判断不可用，沿用火山输入：'+type(exc).__name__
                    # The provider starts its audio-packet deadline on connect.
                    # Finish local cold starts before opening that connection.
                    asr = await open_stream(replace(config, asr_backend=backend), http, cloud)
                    if hasattr(asr, 'report'):
                        asr.report = conversation.record
                from jshi.voice.transcription_recovery import RecoveryIntake, recognize_span
                turn_assessment_job = None
                async def deliver_voice_batch(rows, **arrival):
                    nonlocal turn_assessment_job
                    if online_turn is not None and not hasattr(online_turn, 'coordinator'):
                        for row in rows:
                            await online_turn.submit(row, **arrival)
                        return
                    from jshi.voice.utterance_organizer import coalesce_rows
                    rows = coalesce_rows(rows)
                    states = {}
                    # One budget for the whole batch, not one budget per tiny
                    # speaker fragment. Never stack unfinished model calls.
                    # States only annotate utterance continuation material.
                    if (config.utterance_material and online_turn is not None
                            and (turn_assessment_job is None or turn_assessment_job.done())):
                        samples_by_row = []
                        import numpy as np
                        for row in rows:
                            if row.replaces or row.overlap:
                                continue
                            pcm = audio.segment(max(row.start_ms,row.end_ms-8000),row.end_ms)
                            if pcm:
                                samples_by_row.append((row, np.frombuffer(pcm,dtype='<i2').astype('float32')/32768))
                        def assess_batch():
                            policy = online_turn.coordinator.policy
                            deadline = monotonic()+config.turn_timeout_s+config.semantic_turn_timeout_s
                            result = {}
                            for row,samples in samples_by_row:
                                if monotonic() >= deadline:
                                    break
                                result[(row.start_ms,row.end_ms)] = policy.decide((row,),policy.analyze_audio(samples)).state
                            return result
                        if samples_by_row:
                            turn_assessment_job = asyncio.get_running_loop().run_in_executor(local_executor, assess_batch)
                            turn_assessment_job.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
                            try:
                                states = await asyncio.wait_for(asyncio.shield(turn_assessment_job),
                                    config.turn_timeout_s+config.semantic_turn_timeout_s+.1)
                            except asyncio.TimeoutError:
                                conversation.record('organized_turn_budget', {'parts':len(rows),'state':'unknown'})
                            except Exception as exc:
                                conversation.record('organized_turn_failed', {'error_type':type(exc).__name__})
                    for (start,end),state in states.items():
                        conversation.record('organized_turn_evidence', {'start_ms':start,'end_ms':end,'state':state})
                    with stage(conversation.record, 'voice_batch_delivery',
                               source_ranges=[[r.start_ms, r.end_ms] for r in rows]):
                        await conversation.accept_batch(rows, turn_states=states, **arrival)
                async def recognize_boundary(pcm, offset):
                    return await recognize_span(replace(config,asr_backend=backend),http,cloud,pcm,offset,
                                                report=conversation.record)
                if audio_timeline is not None and config.continuous_analysis == 'active':
                    recovery_intake = RecoveryIntake(audio,recognize_boundary,deliver_voice_batch,conversation.record,
                        identity=conversation.parallel_identity)
                    audio_timeline.on_window = recovery_intake.offer
                async def receive():
                    failed = False
                    identity_pending = None
                    final_received = False
                    def closed_reason():
                        detail = asr.exception() if hasattr(asr, 'exception') else None
                        detail_text = f'{type(detail).__name__}: {detail}' if detail is not None else '远端未提供关闭详情'
                        return f"ASR connection closed (code={getattr(asr, 'close_code', None)}, detail={detail_text})"
                    try:
                        from jshi.voice.streaming_asr import buffered_results
                        async for transcripts, final, received_mono, received_at_ms in buffered_results(asr, report=conversation.record):
                            for transcript in transcripts:
                                report_safely(conversation.record, 'voice_input_stage', {
                                    'stage': 'asr_row_consumed', 'backend': backend,
                                    'source_start_ms': transcript.start_ms, 'source_end_ms': transcript.end_ms,
                                    'asr_received_at_ms': received_at_ms,
                                    'received_to_row_ms': round((monotonic()-received_mono)*1000, 3),
                                    'final': transcript.final})
                                aligned = (transcript,)
                                if transcript.final and (backend == 'bailian' or cloud_alignment is not None):
                                    from jshi.voice.cloud_alignment import unknown, complete_alignment
                                    if cloud_alignment is not None and transcript.replaces:
                                        aligned = cloud_alignment.process(transcript)  # remap existing ranges without audio analysis
                                    elif cloud_alignment is None:
                                        aligned = (unknown(transcript, '多人分段未就绪，不按整段确认人物'),)
                                    else:
                                        async def alignment_slow():
                                            conversation.record('cloud_speaker_alignment_pending', {
                                                'source_start_ms': transcript.start_ms,
                                                'source_end_ms': transcript.end_ms})
                                            await conversation.emit('notice', text='正在区分这段话里的不同声音，完成后再判断接话。')
                                        try:
                                            aligned = await complete_alignment(cloud_alignment, transcript,
                                                local_executor, config.candidate_timeout_s, alignment_slow,
                                                report=conversation.record, trace={'backend': backend,
                                                    'asr_received_at_ms': received_at_ms,
                                                    'received_to_submit_ms': round((monotonic()-received_mono)*1000, 3)})
                                        except Exception as exc:
                                            logging.getLogger(__name__).exception('Cloud speaker alignment failed')
                                            conversation.record('cloud_speaker_alignment_failed', {
                                                'source_start_ms': transcript.start_ms,
                                                'source_end_ms': transcript.end_ms,
                                                'error_type': type(exc).__name__})
                                            aligned = (unknown(transcript, '多人分段失败，不把整段挂到一个人名下'),)
                                    if cloud_alignment is not None and not transcript.replaces:
                                        cloud_alignment.remember(transcript, aligned)
                                    conversation.record('cloud_speaker_alignment', {'backend': backend,
                                        'source_start_ms': transcript.start_ms, 'source_end_ms': transcript.end_ms,
                                        'word_count': len(transcript.words), 'parts': [
                                            {'start_ms': part.start_ms, 'end_ms': part.end_ms,
                                             'track': part.track_id, 'uncertain': part.identity_uncertain}
                                            for part in aligned]})
                                elif speakers is not None and not transcript.replaces:
                                    transcript, identity_pending = await bounded_online_match(audio, transcript, speakers,
                                        local_executor, identity_pending, config.candidate_timeout_s)
                                    aligned = (transcript,)
                                if transcript.final:
                                    from jshi.voice.speaker_evidence import compare_evidence
                                    aligned = compare_evidence(transcript, aligned)
                                    if recovery_intake is not None:
                                        with stage(conversation.record, 'main_reconcile_delivery',
                                                   source_start_ms=transcript.start_ms, source_end_ms=transcript.end_ms,
                                                   asr_received_at_ms=received_at_ms):
                                            await recovery_intake.publish(aligned, source=transcript, asr_received_mono=received_mono,
                                                asr_received_at_ms=received_at_ms,
                                                online_identity_ms=round((monotonic()-received_mono)*1000,1))
                                    else:
                                        await deliver_voice_batch(aligned, asr_received_mono=received_mono,
                                            asr_received_at_ms=received_at_ms)
                                else:
                                    if recovery_intake is not None:
                                        recovery_intake.note_interim(transcript)
                                    await conversation.accept(transcript, asr_received_mono=received_mono,
                                        asr_received_at_ms=received_at_ms)
                            if final:
                                if online_turn:
                                    await online_turn.flush()
                                final_received = True
                                break
                        if not final_received and not ws.closed:
                            raise RuntimeError(closed_reason())
                    except Exception as exc:
                        failed = True
                        logging.getLogger(__name__).exception("Voice ASR receive failed")
                        conversation.record('voice_asr_failure', {'error': str(exc),
                            'error_type': type(exc).__name__, 'close_code': getattr(asr, 'close_code', None)})
                        await conversation.emit("notice", text=f"语音识别失败：{exc}")
                        if conversation.decisions.config.get('speech_hold'):
                            await conversation.apply_schedule_effect(conversation.entry_scheduler.schedule.asr_failed())
                    finally:
                        if not ws.closed:
                            await ws.close(code=1011 if failed else 1000,
                                           message=b'ASR upstream disconnected' if failed else b'')

                if asr is not None:
                    receiver = asyncio.create_task(receive())
                from jshi.voice.diagnostics import watch_event_loop
                loop_watch = asyncio.create_task(watch_event_loop(conversation.record))
                await conversation.emit("ready", session_id=conversation.session_id, identity_mode="local" if use_local else config.identity_mode,
                    continuous_speaker_analysis=('disabled' if config.continuous_analysis == 'off' else
                        ('shadow' if config.continuous_analysis == 'shadow' else 'enabled') if audio_timeline else 'unavailable'),
                    independent_transcription='enabled' if recovery_intake else
                        'disabled' if config.continuous_analysis != 'active' else 'unavailable',
                    acoustic_turn='enabled' if online_turn else 'unavailable' if online_turn_error else 'disabled',
                    acoustic_turn_error=online_turn_error,
                    speaker_split='enabled' if cloud_alignment else 'unavailable' if backend == 'bailian' else 'provider',
                    speaker_split_error=speaker_split_error,
                    voiceprint_available=speakers is not None,
                    voiceprint_count=len(getattr(speakers, 'known', {})) if speakers else 0,
                    saved_references=getattr(speakers, 'saved_reference_counts', lambda: [])() if speakers else [],
                    match_threshold=getattr(speakers, 'threshold', None),
                    single_confirmation_threshold=getattr(speakers, 'confirmation_threshold', None),
                    match_margin=getattr(speakers, 'margin', None),
                    comparison_available=trial is not None,
                    names=[p.label for p in process.profiles.list() if p.status != 'rejected' and p.source != 'voice_scene'][:100],
                    asr=describe_asr(backend, config, use_local),
                    tts="本地固定音色" if tts_backend == 'local' else ("火山双向流式合成" if config.tts_transport == 'websocket' else "在线 SSE 合成"))
                intake_lag_reported = False
                async for message in ws:
                    if message.type == WSMsgType.BINARY:
                        if len(message.data) > 32000 or len(message.data) % 2:
                            raise ValueError("invalid microphone PCM frame")
                        frame = capture_source.accept(message.data)
                        audio.append(frame.pcm)
                        if audio_timeline is not None:
                            audio_timeline.feed()
                        if online_turn:
                            online_turn.feed(frame.pcm)
                        intake_lag_ms = audio.clock_drift_ms()
                        if intake_lag_ms is not None and intake_lag_ms > 3000 and not intake_lag_reported:
                            intake_lag_reported = True
                            logging.getLogger(__name__).warning('cumulative audio clock drift: %.1f s', intake_lag_ms/1000)
                            await conversation.emit('notice', text=f'音频累计时钟偏差约 {intake_lag_ms/1000:.0f} 秒，可能包含采集暂停或传输停顿；不代表本句等待时间，出字耗时按实际收包记录计算。')
                        elif intake_lag_ms is not None and intake_lag_ms < 1000:
                            intake_lag_reported = False
                        if audio.archive_error and not getattr(audio,'archive_error_reported',False):
                            await conversation.emit('notice',text='录音保存失败：'+audio.archive_error)
                            audio.archive_error_reported=True
                        if use_local is not None:
                            local_pump.feed(frame.pcm)
                        else:
                            await asr.send_audio(frame.pcm)
                    elif message.type == WSMsgType.TEXT:
                        payload = message.json()
                        kind = payload.get("type")
                        if kind == 'audio_clock':
                            row = {'at_ms': round(time()*1000), 'session_id': conversation.session_id,
                                   'page_lag_ms': payload.get('page_lag_ms'), 'context_lag_ms': payload.get('context_lag_ms'),
                                   'browser_buffered': payload.get('buffered'),
                                   'audio_clock_drift_ms': audio.clock_drift_ms()}
                            try:
                                with (process.repository.path.parent/'audio_clock.jsonl').open('a', encoding='utf-8') as stream:
                                    stream.write(json.dumps(row)+'\n')
                            except OSError:
                                logging.getLogger(__name__).exception('Audio clock could not be saved')
                        elif kind == 'transcript_displayed':
                            await conversation.transcript_displayed(str(payload.get('input_id', '')), payload.get('render_ms'))
                        elif kind == 'reply_displayed':
                            conversation.record('voice_reply_displayed', {'reply_id':str(payload.get('reply_id','')),
                                'browser_at_ms':payload.get('browser_at_ms'), 'received_at_ms':time()*1000})
                        elif kind == "speech_start":
                            settings = getattr(process, 'model_settings', None)
                            if settings is not None:
                                settings.prewarm()
                            # Energy is only a temporary hold. Sustained speech is a separate signal.
                            conversation.entry_scheduler.schedule.speech_hold = bool(conversation.decisions.config.get('speech_hold'))
                            level = payload.get('level')
                            effect = (conversation.entry_scheduler.schedule.sustained_voice() if level == 'sustained'
                                      else conversation.entry_scheduler.schedule.short_voice())
                            await conversation.apply_schedule_effect(effect)
                        elif kind == "speech_end":
                            schedule = conversation.entry_scheduler.schedule
                            if payload.get('level') == 'sustained':
                                effect = schedule.note_sustained_end()
                                matter = schedule.current()
                                if effect.kind == 'await_asr' and matter is not None:
                                    task = asyncio.create_task(conversation._expire_asr_wait(matter.transient_token, effect.matter_id))
                                    conversation.background_tasks.add(task)
                                    task.add_done_callback(conversation.background_tasks.discard)
                                await conversation.apply_schedule_effect(effect)
                            else:
                                token = schedule.note_short_end()
                                task = asyncio.create_task(conversation._confirm_transient(token, schedule.current_id))
                                conversation.background_tasks.add(task)
                                task.add_done_callback(conversation.background_tasks.discard)
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
                            receipt = {'received_at_ms':time()*1000, 'received_mono':monotonic()}
                            for key in ('client_submitted_at_ms','client_sent_at_ms','client_prepare_ms','client_buffered_bytes'):
                                value = payload.get(key)
                                if type(value) in (int,float) and 0 <= value < 1e15:
                                    receipt[key] = value
                            async def submit_text(text=payload.get('text', ''), oid=payload.get('object_id', ''), receipt=receipt):
                                try:
                                    await conversation.typed(text, oid, receipt=receipt)
                                except Exception as exc:
                                    await conversation.emit('typed_error', text=str(exc))
                            task = asyncio.create_task(submit_text())
                            conversation.background_tasks.add(task)
                            task.add_done_callback(conversation.background_tasks.discard)
                        elif kind == 'person_link_revoke':
                            try:
                                jobs=conversation.revoke_person_link(str(payload.get('link_id','')))
                                await conversation.emit('person_link_updated',jobs=jobs)
                            except ValueError as exc:
                                await conversation.emit('notice',text=str(exc))
                        elif kind == 'rename_confirm':
                            await conversation.apply_rename(str(payload.get('object_id', '')), str(payload.get('name', '')), bool(payload.get('accept')))
                        elif kind == 'personal_label':
                            task = asyncio.create_task(conversation.label_personal_inputs(payload.get('input_ids'), payload.get('object_id', '')))
                            conversation.background_tasks.add(task)
                            task.add_done_callback(conversation.background_tasks.discard)
                        elif kind == 'personal_unlabel':
                            task = asyncio.create_task(conversation.unlabel_personal_inputs(payload.get('input_ids')))
                            conversation.background_tasks.add(task)
                            task.add_done_callback(conversation.background_tasks.discard)
                        elif kind == 'enroll_inputs':
                            task = asyncio.create_task(conversation.enroll_inputs(payload.get('input_ids'), payload.get('name', '')))
                            conversation.background_tasks.add(task)
                            task.add_done_callback(conversation.background_tasks.discard)
                        elif kind == "played":
                            await conversation.acknowledge(payload)
                        elif kind == "end":
                            if asr is not None:
                                await asr.finish()
                                if asr.wait_for_final:
                                    try:
                                        await asyncio.wait_for(asyncio.shield(receiver), timeout=3)
                                    except asyncio.TimeoutError:
                                        pass
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
                if loop_watch:
                    loop_watch.cancel()
                if receiver:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
                actions = []
                if recovery_intake is not None:
                    actions.append(('关闭独立分段识别', recovery_intake.close))
                if audio_timeline is not None:
                    actions.append(('关闭连续声音分析', audio_timeline.close))
                if online_turn is not None:
                    actions.append(('关闭在线声音整句判断', online_turn.close))
                if asr:
                    actions.append(('关闭语音识别连接', asr.close))
                if local_pump:
                    actions.append(('收尾本地语音识别', local_pump.finish))
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
    from .entry_tests import register as register_entry_tests
    register_entry_tests(app, process.repository.path.parent/'entry_test_assets', recent_audio)
    app.router.add_get('/recent-audio',recent_audio_page)
    app.router.add_get('/api/recent-audio',recent_audio_index)
    app.router.add_get('/api/recent-audio-stream/{capture}/{start}.wav',recent_audio_stream)
    app.router.add_get('/api/recent-audio/{iid}.wav',recent_audio_item)
    app.router.add_get('/api/recent-audio/{iid}',recent_audio_item)
    app.router.add_get('/api/voice-context', voice_context_settings)
    app.router.add_post('/api/voice-context', voice_context_settings)
    app.router.add_get("/voice_capture.js", worklet)
    app.router.add_get("/status", status)
    app.router.add_get('/vision_chat.js', vision_chat_script)
    app.router.add_get('/text-people', text_people)
    app.router.add_get('/person-links',person_links)
    app.router.add_post('/person-links',person_links)
    from jshi.app.model_settings import install_model_settings
    def paid_notice(label, model):
        text = (f'{label}：正在调用可能计费的百炼模型（{model}）' if model.startswith('qwen') else
                f'{label}：正在调用付费DeepSeek（{model}）')
        if model_notice_sink is not None:
            model_notice_sink(text)
        if active_conversation is not None:
            asyncio.run_coroutine_threadsafe(active_conversation.emit('model_notice', text=text), active_conversation.worker.get_loop())
    def model_status(event):
        if model_status_sink is not None:
            model_status_sink(event)
        if active_conversation is not None:
            asyncio.run_coroutine_threadsafe(active_conversation.emit('model_status', **event), active_conversation.worker.get_loop())
    install_model_settings(app, process, jev, paid_notice, model_status)
    app.router.add_post('/text-input', text_input)
    from jshi.app.web_data import register_data_routes
    register_data_routes(app, process, subject_id)
    app.router.add_get("/voice", connection)
    app.router.add_get('/voice-enroll', enrollment_page)
    app.router.add_get('/voice-registry', registry)
    app.router.add_post('/voice-registry', registry)
    from jshi.app.personal_speakers import install_personal_routes
    async def personal_features(model):
        if model:
            if not speaker_models or model not in speaker_models:
                raise ValueError('未知声音特征模型')
            return await asyncio.to_thread(speaker_models[model])
        if active_conversation is not None:
            return active_conversation.local_speakers
        return online_speakers or (local.speakers if local else None)
    install_personal_routes(app, personal_speakers, process.profiles, personal_features)
    if getattr(process, 'vision', None):
        from jshi.vision.web import install_vision
        install_vision(app, process, subject_id, deliver_environment, voice_active=lambda: active_conversation is not None)
    return app


def run_voice(process, identities, args) -> None:
    from jshi.app.cli import _interactive_subject_id
    subject_id = _interactive_subject_id(args.data_dir, args.subject_id, identities)
    try:
        from aiohttp import web
    except ImportError:
        raise SystemExit('语音入口需要安装：python -m pip install -e ".[voice]"')
    config = VoiceConfig.from_env(asr=args.asr, tts=args.tts)
    from jshi.core.skillconfig import SkillConfigStore, build_model_port
    from jshi.app.cli import _skills_config_path
    store = SkillConfigStore()
    path = _skills_config_path()
    if path:
        store.load_file(path)
    jev = VoiceJEV(build_model_port(store.profile("voice_jev")))
    local = None
    if config.asr_backend == "local":
        from jshi.voice.runtime import build_local
        local = build_local(args.data_dir, process.profiles)
    elif config.asr_backend == 'qwen':
        from jshi.voice.runtime import build_qwen
        local = build_qwen(args.data_dir, process.profiles, config)
    local_tts = None
    if config.tts_backend == "local":
        from jshi.voice.local_tts import build_local_tts
        local_tts = build_local_tts(args.data_dir, config)
    online_speakers = None
    if local is None:
        from jshi.voice.runtime import build_speakers
        online_speakers = build_speakers(args.data_dir, process.profiles)
    from jshi.voice.runtime import build_speakers
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
    backend = getattr(process.memory, '_backend', process.memory)
    prepare = getattr(backend, 'prepare_local_recall', None)
    if prepare:
        print('正在准备本地记忆检索模型，准备完成后开放对话入口…')
        prepare()
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
