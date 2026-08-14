from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Sequence

from jshi.activezone import (
    ActiveZoneEvent,
    ActiveZonePort,
    ActiveZoneView,
    InProcessActiveZone,
)
from jshi.attention import ChancePort, PlaceholderChance
from jshi.core import Provenance, SubjectState
from jshi.feedback import PlaceholderResultFeedback, ResultFeedbackPort
from jshi.governance import (
    ContinuityCheckPort,
    ContinuityFinding,
    PlaceholderContinuityCheck,
)
from jshi.identity import IdentityRepository
from jshi.intent import IntentPort, PlaceholderIntent
from jshi.memory import InProcessHistoryMemory, MemoryPort, RecalledFragment
from jshi.models import ModelPort, ModelRequest, ObjectAssessment, RecallRequest
from jshi.personalworld import InProcessPersonalWorld, PersonalWorldPort
from jshi.recognition import (
    CarrierEntry,
    MIN_OBJECT_CONFIDENCE,
    ObjectProfile,
    ObjectProfileRepository,
    ObjectRecognitionPort,
    ProfileObjectRecognition,
    SpeakerCandidate,
)
from jshi.reflection import PlaceholderReflection, ReflectionPort

from .domain import (
    ALLOWED_EPISTEMIC_TRANSITIONS,
    Activity,
    ActivityKind,
    ActivityStatus,
    CognitiveContent,
    CognitiveKind,
    EpistemicStatus,
    EvidenceKind,
    HistoryKind,
    HistoryRecord,
    PersonalItem,
    PersonalKind,
    PersonalStatus,
    StateTransition,
)
from .repository import SubjectRepository


# 追加召回占位预算：每活动最多轮次、每轮默认片段数。
FOLLOWUP_RECALL_MAX_ROUNDS = 2
FOLLOWUP_RECALL_DEFAULT_LIMIT = 3


@dataclass(frozen=True)
class AssembledCurrentState:
    """Pre-model working set. Never invents subject-facing open matter."""

    input_text: str
    subject_state: SubjectState
    personal_items: tuple[PersonalItem, ...]
    active_zone: ActiveZoneView
    active_event_ids: tuple[str, ...]
    recalled: tuple[RecalledFragment, ...]


@dataclass(frozen=True)
class SubjectPreview:
    """preview-state 输出：对象解析 + 活跃区装载 + 组装快照（只读，不落库）。"""

    speaker: SpeakerCandidate
    active_zone: ActiveZoneView
    assembled: AssembledCurrentState


@dataclass(frozen=True)
class SubjectActivityResult:
    activity: Activity
    perception: CognitiveContent
    thought: CognitiveContent
    action_text: str
    current_state: AssembledCurrentState
    speaker: SpeakerCandidate = field(default_factory=SpeakerCandidate)


class SubjectProcess:
    """最小主体循环：识别→落位→活跃区装载→组装→活动→认知(可追加召回)→行动→收尾。

    每个系统都是占位接口：身份识别、活跃区、意图、受约束偶然性、结果反馈、
    治理检验、反思。各系统可独立替换为真实实现，不改主流程顺序。
    归属判断已废弃（02 起不再接线），事件聚焦由模型在认知阶段完成。
    """

    def __init__(
        self,
        repository: SubjectRepository,
        identities: IdentityRepository,
        cognition: ModelPort,
        memory: MemoryPort | None = None,
        personal_world: PersonalWorldPort | None = None,
        recognition: ObjectRecognitionPort | None = None,
        attribution: object | None = None,  # 已废弃：仅保留位置兼容，主流程不再使用
        active_zone: ActiveZonePort | None = None,
        intent: IntentPort | None = None,
        feedback: ResultFeedbackPort | None = None,
        governance: ContinuityCheckPort | None = None,
        chance: ChancePort | None = None,
        reflection: ReflectionPort | None = None,
    ) -> None:
        self.repository = repository
        self.identities = identities
        self.cognition = cognition
        self.memory = memory or InProcessHistoryMemory(repository)
        self.personal_world = personal_world or InProcessPersonalWorld(repository)
        self.profiles = ObjectProfileRepository(repository.path)
        self.recognition = recognition or ProfileObjectRecognition(
            self.profiles, memory_matcher=self._match_object_by_memory
        )
        self.active_zone = active_zone or InProcessActiveZone(repository)
        self.intent = intent or PlaceholderIntent()
        self.feedback = feedback or PlaceholderResultFeedback()
        self.governance = governance or PlaceholderContinuityCheck()
        self.chance = chance or PlaceholderChance()
        self.reflection = reflection or PlaceholderReflection(self)

    # ------------------------------------------------------------------
    # 阶段②/③ 前置：活跃区装载与单一路径组装
    # ------------------------------------------------------------------

    def assemble_current_state(
        self,
        subject_id: str,
        input_text: str,
        view: ActiveZoneView | None = None,
    ) -> AssembledCurrentState:
        """单一路径组装（只读已有记录）：
        活跃区事件 + 承诺 + 个人世界 + 当前输入；不调用模型、不进行文本召回。
        """
        view = view or self.active_zone.load(subject_id, input_text)
        selected = tuple(self.personal_world.select(subject_id, input_text))
        personal = tuple(
            item for item in selected if item.kind is not PersonalKind.CONCERN
        )
        unfinished = tuple(
            event for event in view.events if event.status == "unfinished"
        )
        subject_state = self._subject_state(subject_id, personal, unfinished)
        assembled = AssembledCurrentState(
            input_text=input_text,
            subject_state=subject_state,
            personal_items=personal,
            active_zone=view,
            active_event_ids=tuple(event.event_id for event in view.events),
            recalled=(),
        )
        return self.chance.apply("assemble", assembled)

    def recall_events(
        self, subject_id: str, event_ids: Sequence[str]
    ) -> tuple[RecalledFragment, ...]:
        """显式事件 id 直接装载/召回（程序侧窄接口），并记账 event_recalled。

        事件 id 由输入信封或活动上下文显式携带（未来）；本期 CLI 未暴露，
        接口保留给后续输入契约扩展。
        """
        if not event_ids:
            return ()
        fragments = self.active_zone.recall_by_ids(subject_id, event_ids)
        if fragments:
            self.repository.add_history(
                HistoryRecord(
                    subject_id=subject_id,
                    kind=HistoryKind.SUBJECT,
                    event_type="event_recalled",
                    content={
                        "event_ids": [item.event_id for item in fragments],
                    },
                    source_ids=tuple(item.event_id for item in fragments),
                )
            )
        return fragments

    def preview_state(
        self,
        subject_id: str,
        input_text: str,
        *,
        object_ref: str | None = None,
        channel: str | None = None,
        carriers: tuple[CarrierEntry, ...] = (),
    ) -> SubjectPreview:
        """预览进模型前的当前状态（只读，不落库、不调用模型）。"""
        self._require_object_source(object_ref, channel, carriers)
        speaker = self.recognition.resolve(
            subject_id, input_text, object_ref, channel, carriers
        )
        view = self.active_zone.load(subject_id, input_text)
        assembled = self.assemble_current_state(subject_id, input_text, view)
        return SubjectPreview(speaker=speaker, active_zone=view, assembled=assembled)

    def _subject_state(
        self,
        subject_id: str,
        personal: Sequence[PersonalItem],
        unfinished_events: Sequence[ActiveZoneEvent],
    ) -> SubjectState:
        identity = self.identities.get(subject_id)
        values = tuple(
            item.content for item in personal if item.kind is PersonalKind.VALUE
        )
        commitments = tuple(
            item.content for item in personal if item.kind is PersonalKind.COMMITMENT
        )
        concerns = tuple(event.content for event in unfinished_events)
        return SubjectState(
            subject_id=subject_id,
            identity_summary=f"{identity.name}；来源：{identity.origin}",
            current_stance=identity.narrative,
            salient_values=values,
            commitments=commitments,
            concerns=concerns,
            provenance=Provenance(
                source="assembled_current_state",
                method="load_existing_only",
            ),
        )

    # ------------------------------------------------------------------
    # 外部活动主流程
    # ------------------------------------------------------------------

    def experience(
        self,
        subject_id: str,
        text: str,
        *,
        object_ref: str | None = None,
        channel: str | None = None,
        carriers: tuple[CarrierEntry, ...] = (),
    ) -> SubjectActivityResult:
        # 阶段① 对象必填校验 → 对象解析 → 置信度门禁 → 落位
        self._require_object_source(object_ref, channel, carriers)
        speaker = self.recognition.resolve(
            subject_id, text, object_ref, channel, carriers
        )
        if not speaker.object_id:
            raise ValueError("object candidate must be referenceable (object_id)")
        if speaker.confidence < MIN_OBJECT_CONFIDENCE:
            self.repository.add_history(
                HistoryRecord(
                    subject_id=subject_id,
                    kind=HistoryKind.SUBJECT,
                    event_type="object_rejected",
                    content={
                        "object_ref": object_ref,
                        "confidence": speaker.confidence,
                        "reason": speaker.reason
                        or (
                            f"object confidence {speaker.confidence:.2f} "
                            f"below threshold {MIN_OBJECT_CONFIDENCE}"
                        ),
                    },
                    source_ids=(),
                )
            )
            raise ValueError(
                f"object confidence too low: {speaker.confidence:.2f}"
            )
        fact = HistoryRecord(
            subject_id=subject_id,
            kind=HistoryKind.FACT,
            event_type="external_input",
            content={
                "text": text,
                "source": speaker.label,
                "object_id": speaker.object_id,
                "object_status": speaker.status,
                "object_confidence": speaker.confidence,
                "object_ref": object_ref,
                "channel": channel,
            },
        )
        self.repository.add_history(fact)

        # 新对象落库（通过门禁后）：暂定档案，来源 = 输入事实 id
        if self.profiles.get(speaker.object_id) is None:
            self.profiles.create(
                ObjectProfile(
                    object_id=speaker.object_id,
                    label=speaker.label,
                    carriers=speaker.carriers,
                    source=fact.id,
                    status="provisional",
                )
            )
            self.repository.add_history(
                HistoryRecord(
                    subject_id=subject_id,
                    kind=HistoryKind.SUBJECT,
                    event_type="object_resolved",
                    content={
                        "object_id": speaker.object_id,
                        "label": speaker.label,
                        "source": fact.id,
                        "status": "provisional",
                    },
                    source_ids=(fact.id,),
                )
            )

        # 阶段② 活跃区装载（只装载，不调整；不调用模型）
        view = self.active_zone.load(subject_id, text)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="event_loaded",
                content={
                    "input": text,
                    "event_ids": [event.event_id for event in view.events],
                    "unfinished_ids": [
                        event.event_id
                        for event in view.events
                        if event.status == "unfinished"
                    ],
                    "completed_ids": [
                        event.event_id
                        for event in view.events
                        if event.status == "completed"
                    ],
                    "budget": view.budget,
                },
                source_ids=(fact.id, *(event.event_id for event in view.events)),
            )
        )

        # 阶段③ 当前状态组装（单一路径，只读已有记录）
        current = self.assemble_current_state(subject_id, text, view)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="current_state_assembled",
                content={
                    "input": text,
                    "event_ids": list(current.active_event_ids),
                    "budget": view.budget,
                    "value_count": len(current.subject_state.salient_values),
                    "commitment_count": len(current.subject_state.commitments),
                    "recalled_event_ids": [
                        item.event_id for item in current.recalled
                    ],
                },
                source_ids=(fact.id, *current.active_event_ids),
            )
        )

        # 阶段④ 活动建立（事件挂载在认知后由模型聚焦回填）
        activity = Activity(
            subject_id=subject_id,
            kind=ActivityKind.EXTERNAL,
            trigger=fact.id,
            active_concern_ids=(),
        )
        self.repository.add_activity(activity)
        self.intent.begin(activity)
        intent_ids = self.intent.resolve(activity, text, ())
        if intent_ids:
            activity = self.repository.update_activity(
                activity.id, intention_ids=intent_ids
            )

        # 感知（已接受的他人报告）
        perception = CognitiveContent(
            subject_id=subject_id,
            activity_id=activity.id,
            kind=CognitiveKind.PERCEPTION,
            content=f"对方表达：{text}",
            epistemic_status=EpistemicStatus.ACCEPTED,
            evidence_kind=EvidenceKind.REPORT,
            source_ids=(fact.id,),
        )
        self.repository.add_cognitive_content(perception)

        # 阶段⑤ 认知活动（多轮，可追加召回；模型聚焦事件）
        thought, response = self._cognize(
            subject_id, activity, current, perception
        )
        if response.object_assessment is not None:
            speaker = self._apply_object_assessment(
                subject_id, speaker, fact, thought, response.object_assessment
            )
        if response.focused_event_ids:
            activity = self._attach_focused_events(
                subject_id, activity, response.focused_event_ids
            )

        # 阶段⑥ 行动与结果
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.FACT,
                event_type="language_action",
                content={
                    "activity_id": activity.id,
                    "text": thought.content,
                    "model": thought.model,
                },
                source_ids=(thought.id,),
            )
        )
        self.feedback.ingest_result(subject_id, activity.id, thought.content)

        # 阶段⑦ 收尾与沉淀：活跃区调整（剔除）→ 活动完成 → 治理
        evicted = self.active_zone.evict_overflow(subject_id, view)
        for event in evicted:
            self.repository.add_history(
                HistoryRecord(
                    subject_id=subject_id,
                    kind=HistoryKind.SUBJECT,
                    event_type="event_evicted",
                    content={
                        "event_id": event.event_id,
                        "content": event.content,
                        "status": event.status,
                        "reason": "budget_overflow",
                    },
                    source_ids=(event.event_id,),
                )
            )
        completed = self.repository.update_activity(
            activity.id, status=ActivityStatus.COMPLETED
        )
        findings = self.governance.check(subject_id, completed, fact, None)
        if findings:
            self.repository.add_history(
                HistoryRecord(
                    subject_id=subject_id,
                    kind=HistoryKind.SUBJECT,
                    event_type="continuity_check",
                    content={
                        "findings": [
                            {
                                "severity": finding.severity,
                                "message": finding.message,
                                "evidence_ids": list(finding.evidence_ids),
                            }
                            for finding in findings
                        ]
                    },
                    source_ids=(fact.id,),
                )
            )
        return SubjectActivityResult(
            completed,
            perception,
            thought,
            thought.content,
            current,
            speaker=speaker,
        )

    def _attach_focused_events(
        self,
        subject_id: str,
        activity: Activity,
        event_ids: Sequence[str],
    ) -> Activity:
        """认知后回填活动挂载的事件 id（只接受库中存在的事件）。"""
        existing = {
            item.id
            for item in self.repository.list_personal_items(
                subject_id,
                kind=PersonalKind.CONCERN,
                active_only=False,
            )
        }
        valid = tuple(dict.fromkeys(id_ for id_ in event_ids if id_ in existing))
        if not valid:
            return activity
        activity = self.repository.update_activity(
            activity.id, active_concern_ids=valid
        )
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="activity_events_attached",
                content={
                    "activity_id": activity.id,
                    "event_ids": list(valid),
                    "basis": "model_focus",
                },
                source_ids=valid,
            )
        )
        return activity

    def _cognize(
        self,
        subject_id: str,
        activity: Activity,
        current: AssembledCurrentState,
        perception: CognitiveContent,
    ) -> tuple[CognitiveContent, object]:
        """认知活动：模型可提出追加召回请求，程序执行后继续认知，直至产出或预算耗尽。"""
        working_personal = current.personal_items
        working_recalled: list[RecalledFragment] = list(current.recalled)
        response = None
        rounds = 0
        while True:
            response = self.cognition.generate(
                ModelRequest(
                    purpose="subject_activity",
                    input_text=current.input_text,
                    subject_state=current.subject_state,
                    context=self._model_context(
                        working_personal,
                        working_recalled,
                        current.active_zone.events,
                    ),
                )
            )
            requests: tuple[RecallRequest, ...] = response.recall_requests or ()
            if requests and rounds < FOLLOWUP_RECALL_MAX_ROUNDS:
                for request in requests:
                    fragments = self.memory.recall(
                        subject_id,
                        request.query,
                        limit=request.budget or FOLLOWUP_RECALL_DEFAULT_LIMIT,
                    )
                    known = {item.event_id for item in working_recalled}
                    fresh = [item for item in fragments if item.event_id not in known]
                    if fresh:
                        working_recalled.extend(fresh)
                        self.repository.add_history(
                            HistoryRecord(
                                subject_id=subject_id,
                                kind=HistoryKind.SUBJECT,
                                event_type="recall_extended",
                                content={
                                    "activity_id": activity.id,
                                    "request": {
                                        "query": request.query,
                                        "budget": request.budget,
                                        "object_ids": list(request.object_ids),
                                        "anchor_event_ids": list(
                                            request.anchor_event_ids
                                        ),
                                    },
                                    "recalled_event_ids": [
                                        item.event_id for item in fresh
                                    ],
                                },
                                source_ids=(
                                    perception.id,
                                    *(item.event_id for item in fresh),
                                ),
                            )
                        )
                rounds += 1
                continue
            break

        thought = CognitiveContent(
            subject_id=subject_id,
            activity_id=activity.id,
            kind=CognitiveKind.INFERENCE,
            content=response.text,
            epistemic_status=EpistemicStatus.CONSIDERING,
            evidence_kind=EvidenceKind.COGNITIVE_REASONING,
            source_ids=(
                perception.id,
                *(item.id for item in working_personal),
                *(event.event_id for event in current.active_zone.events),
                *(item.event_id for item in working_recalled),
            ),
            model=response.model,
        )
        self.repository.add_cognitive_content(thought)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="cognitive_content_appeared",
                content={
                    "activity_id": activity.id,
                    "cognitive_content_id": thought.id,
                    "content": thought.content,
                    "epistemic_status": thought.epistemic_status.value,
                },
                source_ids=thought.source_ids,
            )
        )
        return thought, response

    def _apply_object_assessment(
        self,
        subject_id: str,
        speaker: SpeakerCandidate,
        fact: HistoryRecord,
        thought: CognitiveContent,
        assessment: ObjectAssessment,
    ) -> SpeakerCandidate:
        """认知确认：记录判定并更新对象档案状态（追加记录，不改写事实原文）。"""
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="object_identity_assessed",
                content={
                    "candidate_label": assessment.label or speaker.label,
                    "object_id": assessment.object_id or speaker.object_id,
                    "conclusion": assessment.conclusion,
                    "reason": assessment.reason,
                    "cognitive_content_id": thought.id,
                    "epistemic_status": thought.epistemic_status.value,
                },
                source_ids=(thought.id,),
            )
        )
        updated = speaker
        if assessment.conclusion == "confirm":
            self.profiles.update_status(speaker.object_id, "confirmed")
            updated = replace(
                speaker, status="confirmed", confidence=max(speaker.confidence, 0.85)
            )
        elif assessment.conclusion == "deny":
            self.profiles.update_status(speaker.object_id, "rejected")
            updated = replace(speaker, status="rejected", confidence=0.0)
        return updated

    def _match_object_by_memory(
        self,
        subject_id: str,
        text: str,
        candidates: tuple[ObjectProfile, ...],
    ) -> tuple[ObjectProfile, float] | None:
        """重名消歧占位：按对象过滤事实历史，与输入做词重叠打分，返回最高者。"""
        facts = self.repository.list_history(subject_id, kind=HistoryKind.FACT)
        bigrams = {
            text.lower()[index : index + 2]
            for index in range(len(text) - 1)
        }
        best: ObjectProfile | None = None
        best_score = 0.0
        for candidate in candidates:
            related = [
                record
                for record in facts
                if record.content.get("object_id") == candidate.object_id
            ]
            hay = " ".join(
                str(record.content.get("text", "")) for record in related
            ).lower()
            score = sum(1 for gram in bigrams if gram in hay)
            if score > best_score:
                best, best_score = candidate, float(score)
        if best is not None and best_score > 0:
            return best, best_score
        return None

    @staticmethod
    def _require_object_source(
        object_ref: str | None,
        channel: str | None,
        carriers: tuple[CarrierEntry, ...] = (),
    ) -> None:
        if (
            not (object_ref and object_ref.strip())
            and not channel
            and not carriers
        ):
            raise ValueError(
                "invalid input envelope: external input requires an object "
                "reference (object_ref / channel / carriers)"
            )

    @staticmethod
    def _model_context(
        personal: Sequence[PersonalItem],
        recalled: Sequence[RecalledFragment],
        active_events: Sequence[ActiveZoneEvent] = (),
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "id": event.event_id,
                "kind": "event",
                "content": event.content,
                "status": event.status,
            }
            for event in active_events
        ) + tuple(
            {
                "id": item.id,
                "kind": item.kind.value,
                "content": item.content,
                "status": item.status.value,
            }
            for item in personal
        ) + tuple(
            {
                "id": item.event_id,
                "kind": "recalled_fact",
                "content": item.text,
                "status": "active",
                "event_type": item.event_type,
            }
            for item in recalled
        )

    # ------------------------------------------------------------------
    # 反思（内部活动，委托给反思系统）
    # ------------------------------------------------------------------

    def reflect(self, subject_id: str, prompt: str) -> CognitiveContent:
        return self.reflection.reflect(subject_id, prompt)

    def _reflect_internal(self, subject_id: str, prompt: str) -> CognitiveContent:
        current = self.assemble_current_state(subject_id, prompt)
        recent_history = self.repository.list_history(subject_id, limit=20)
        activity = Activity(
            subject_id=subject_id,
            kind=ActivityKind.INTERNAL,
            trigger=prompt,
            active_concern_ids=current.active_event_ids,
        )
        self.repository.add_activity(activity)
        history_text = "\n".join(
            f"{item.kind.value}:{item.event_type}:{dict(item.content)}"
            for item in recent_history
        )
        response = self.cognition.generate(
            ModelRequest(
                purpose="reflection",
                input_text=f"{prompt}\n近期历史：\n{history_text}",
                subject_state=current.subject_state,
                context=tuple(
                    {"kind": item.kind.value, "content": item.content}
                    for item in current.personal_items
                ),
            )
        )
        reflection = CognitiveContent(
            subject_id=subject_id,
            activity_id=activity.id,
            kind=CognitiveKind.EVALUATION,
            content=response.text,
            epistemic_status=EpistemicStatus.CONSIDERING,
            evidence_kind=EvidenceKind.COGNITIVE_REASONING,
            source_ids=tuple(item.id for item in recent_history),
            model=response.model,
        )
        self.repository.add_cognitive_content(reflection)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="reflection",
                content={
                    "activity_id": activity.id,
                    "cognitive_content_id": reflection.id,
                    "content": reflection.content,
                    "epistemic_status": reflection.epistemic_status.value,
                },
                source_ids=reflection.source_ids,
            )
        )
        self.repository.update_activity(activity.id, status=ActivityStatus.COMPLETED)
        return reflection

    # ------------------------------------------------------------------
    # 状态迁移与个人内容
    # ------------------------------------------------------------------

    def transition_cognition(
        self,
        content_id: str,
        to_status: EpistemicStatus,
        reason: str,
        source_ids: tuple[str, ...] = (),
    ) -> CognitiveContent:
        current = self.repository.get_cognitive_content(content_id)
        allowed = ALLOWED_EPISTEMIC_TRANSITIONS[current.epistemic_status]
        if to_status not in allowed:
            raise ValueError(
                f"Invalid epistemic transition: "
                f"{current.epistemic_status.value} -> {to_status.value}"
            )
        transition = StateTransition(
            subject_id=current.subject_id,
            target_type="cognitive_content",
            target_id=current.id,
            from_state=current.epistemic_status.value,
            to_state=to_status.value,
            reason=reason,
            source_ids=source_ids,
        )
        updated = self.repository.update_epistemic_status(content_id, to_status)
        self.repository.add_transition(transition)
        self.repository.add_history(
            HistoryRecord(
                subject_id=current.subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="epistemic_transition",
                content={
                    "cognitive_content_id": content_id,
                    "from": transition.from_state,
                    "to": transition.to_state,
                    "reason": reason,
                },
                source_ids=(transition.id, *source_ids),
            )
        )
        return updated

    def add_personal_item(
        self,
        subject_id: str,
        kind: PersonalKind,
        content: str,
        source_ids: tuple[str, ...] = (),
        importance: float = 1.0,
    ) -> PersonalItem:
        metadata: dict[str, object] = {}
        if importance != 1.0:
            metadata["importance"] = importance
        item = PersonalItem(
            subject_id=subject_id,
            kind=kind,
            content=content,
            source_ids=source_ids,
            metadata=metadata,
        )
        self.repository.add_personal_item(item)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="personal_item_created",
                content={
                    "personal_item_id": item.id,
                    "kind": item.kind.value,
                    "content": item.content,
                },
                source_ids=source_ids,
            )
        )
        return item

    def propose_open_matter(
        self,
        subject_id: str,
        content: str,
        *,
        source_ids: tuple[str, ...],
    ) -> PersonalItem:
        """Record subject-facing open matter after cognition, with required sources.

        This is never called by assemble_current_state. Call it only once a
        cognitive result (or human judgment) has identified unfinished follow-through.
        """
        if not source_ids:
            raise ValueError(
                "Subject-facing open matter requires source_ids "
                "(cognitive content or fact ids)"
            )
        return self.add_personal_item(
            subject_id, PersonalKind.CONCERN, content, source_ids=source_ids
        )

    def close_personal_item(
        self, item_id: str, status: PersonalStatus, reason: str
    ) -> PersonalItem:
        if status is PersonalStatus.ACTIVE:
            raise ValueError("Closing a personal item requires a non-active status")
        current = self.repository.get_personal_item(item_id)
        updated = self.repository.update_personal_status(item_id, status)
        transition = StateTransition(
            subject_id=current.subject_id,
            target_type="personal_item",
            target_id=item_id,
            from_state=current.status.value,
            to_state=status.value,
            reason=reason,
        )
        self.repository.add_transition(transition)
        self.repository.add_history(
            HistoryRecord(
                subject_id=current.subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="personal_item_closed",
                content={
                    "personal_item_id": item_id,
                    "kind": current.kind.value,
                    "to": status.value,
                    "reason": reason,
                },
                source_ids=(transition.id,),
            )
        )
        return updated
