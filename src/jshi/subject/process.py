from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Sequence

from jshi.attention import ChancePort, PlaceholderChance
from jshi.attribution import AttributionPort, AttributionResult, PlaceholderAttribution
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
    open_matter_ids: tuple[str, ...]
    recalled: tuple[RecalledFragment, ...]


@dataclass(frozen=True)
class SubjectPreview:
    """preview-state 输出：身份识别 + 归属判断 + 组装快照（只读，不落库）。"""

    speaker: SpeakerCandidate
    attribution: AttributionResult
    mode: str  # concern_centric | full
    assembled: AssembledCurrentState


@dataclass(frozen=True)
class SubjectActivityResult:
    activity: Activity
    perception: CognitiveContent
    thought: CognitiveContent
    action_text: str
    current_state: AssembledCurrentState
    speaker: SpeakerCandidate = field(default_factory=SpeakerCandidate)
    attribution: AttributionResult = field(default_factory=AttributionResult)


class SubjectProcess:
    """最小主体循环：识别→落位→归属→组装→活动→认知(可追加召回)→行动→收尾。

    每个系统都是占位接口：身份识别、归属判断、意图、受约束偶然性、结果反馈、
    治理检验、反思。各系统可独立替换为真实实现，不改主流程顺序。
    """

    def __init__(
        self,
        repository: SubjectRepository,
        identities: IdentityRepository,
        cognition: ModelPort,
        memory: MemoryPort | None = None,
        personal_world: PersonalWorldPort | None = None,
        recognition: ObjectRecognitionPort | None = None,
        attribution: AttributionPort | None = None,
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
        self.attribution = attribution or PlaceholderAttribution()
        self.intent = intent or PlaceholderIntent()
        self.feedback = feedback or PlaceholderResultFeedback()
        self.governance = governance or PlaceholderContinuityCheck()
        self.chance = chance or PlaceholderChance()
        self.reflection = reflection or PlaceholderReflection(self)

    # ------------------------------------------------------------------
    # 阶段②/③ 前置：常驻约束、归属判断、双路组装
    # ------------------------------------------------------------------

    def _standing_constraints(self, subject_id: str) -> tuple[PersonalItem, ...]:
        """常驻约束清单：承诺 + 主体面未完成现实（供归属判断与始终装载）。"""
        return tuple(self.personal_world.standing_constraints(subject_id))

    def _recent_activity(self, subject_id: str) -> Activity | None:
        activities = self.repository.list_activities(subject_id)
        return activities[-1] if activities else None

    def _judge_attribution(
        self, subject_id: str, input_text: str
    ) -> tuple[AttributionResult, tuple[PersonalItem, ...]]:
        standing = self._standing_constraints(subject_id)
        result = self.attribution.judge(
            subject_id, input_text, standing, self._recent_activity(subject_id)
        )
        return result, standing

    def assemble_current_state(
        self, subject_id: str, input_text: str
    ) -> AssembledCurrentState:
        """全量组装（未命中关切）：身份 + 个人世界 + 开放事项清单 + 记忆召回。"""
        personal = tuple(self.personal_world.select(subject_id, input_text))
        open_matter = tuple(
            item for item in personal if item.kind is PersonalKind.CONCERN
        )
        recalled = tuple(self.memory.recall(subject_id, input_text))
        subject_state = self._subject_state(subject_id, personal)
        assembled = AssembledCurrentState(
            input_text=input_text,
            subject_state=subject_state,
            personal_items=personal,
            open_matter_ids=tuple(item.id for item in open_matter),
            recalled=recalled,
        )
        return self.chance.apply("assemble", assembled)

    def _assemble_concern_centric(
        self, subject_id: str, input_text: str, concern_ids: tuple[str, ...]
    ) -> AssembledCurrentState:
        """关切中心组装（命中关切）：焦点关切来源与推进史 + 承诺 + 截断的个人世界。"""
        standing = self._standing_constraints(subject_id)
        focus = tuple(
            item
            for item in standing
            if item.id in concern_ids and item.kind is PersonalKind.CONCERN
        )
        query = " ".join(
            [input_text, *(item.content for item in focus)]
        ).strip()
        personal = tuple(self.personal_world.select(subject_id, query))
        recalled = tuple(self.memory.recall(subject_id, query))
        subject_state = self._subject_state(subject_id, personal)
        assembled = AssembledCurrentState(
            input_text=input_text,
            subject_state=subject_state,
            personal_items=personal,
            open_matter_ids=tuple(item.id for item in focus),
            recalled=recalled,
        )
        return self.chance.apply("assemble", assembled)

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
        attribution, _ = self._judge_attribution(subject_id, input_text)
        if attribution.concern_ids:
            assembled = self._assemble_concern_centric(
                subject_id, input_text, attribution.concern_ids
            )
            mode = "concern_centric"
        else:
            assembled = self.assemble_current_state(subject_id, input_text)
            mode = "full"
        return SubjectPreview(
            speaker=speaker, attribution=attribution, mode=mode, assembled=assembled
        )

    def _subject_state(
        self, subject_id: str, personal: Sequence[PersonalItem]
    ) -> SubjectState:
        identity = self.identities.get(subject_id)
        values = tuple(
            item.content for item in personal if item.kind is PersonalKind.VALUE
        )
        concerns = tuple(
            item.content for item in personal if item.kind is PersonalKind.CONCERN
        )
        commitments = tuple(
            item.content for item in personal if item.kind is PersonalKind.COMMITMENT
        )
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

        # 阶段② 归属判断（结果暂定，可修正）
        attribution, _ = self._judge_attribution(subject_id, text)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="input_attributed",
                content={
                    "input": text,
                    "concern_ids": list(attribution.concern_ids),
                    "confidence": attribution.confidence,
                    "basis": list(attribution.basis),
                    "status": attribution.status,
                },
                source_ids=(fact.id, *attribution.concern_ids),
            )
        )

        # 阶段③ 当前状态组装（两条支路，均只读已有记录）
        if attribution.concern_ids:
            current = self._assemble_concern_centric(
                subject_id, text, attribution.concern_ids
            )
            mode = "concern_centric"
        else:
            current = self.assemble_current_state(subject_id, text)
            mode = "full"
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="current_state_assembled",
                content={
                    "input": text,
                    "mode": mode,
                    "attributed_concern_ids": list(attribution.concern_ids),
                    "open_matter_ids": list(current.open_matter_ids),
                    "value_count": len(current.subject_state.salient_values),
                    "commitment_count": len(current.subject_state.commitments),
                    "recalled_event_ids": [
                        item.event_id for item in current.recalled
                    ],
                },
                source_ids=(fact.id, *attribution.concern_ids),
            )
        )

        # 阶段④ 活动建立（挂载命中的关切 id）
        activity = Activity(
            subject_id=subject_id,
            kind=ActivityKind.EXTERNAL,
            trigger=fact.id,
            active_concern_ids=attribution.concern_ids,
        )
        self.repository.add_activity(activity)
        self.intent.begin(activity)
        intent_ids = self.intent.resolve(activity, text, attribution.concern_ids)
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

        # 阶段⑤ 认知活动（多轮，可追加召回）
        thought, response = self._cognize(
            subject_id, activity, current, perception
        )
        if response.object_assessment is not None:
            speaker = self._apply_object_assessment(
                subject_id, speaker, fact, thought, response.object_assessment
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

        # 阶段⑦ 收尾与沉淀
        completed = self.repository.update_activity(
            activity.id, status=ActivityStatus.COMPLETED
        )
        findings = self.governance.check(subject_id, completed, fact, attribution)
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
            attribution=attribution,
        )

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
                    context=self._model_context(working_personal, working_recalled),
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
    ) -> tuple[dict[str, object], ...]:
        return tuple(
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
            active_concern_ids=current.open_matter_ids,
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
