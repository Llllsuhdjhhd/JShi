from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Sequence

from jshi.activezone import (
    ActiveZonePort,
    InProcessActiveZone,
)
from jshi.action import (
    InProcessActionRouter,
    PlaceholderRobotAction,
    PlaceholderSpeech,
)
from jshi.activityclose import InProcessActivityClose
from jshi.responsemark import InProcessResponseMark
from jshi.experienceledger import (
    ContextViewState,
    ExperienceLedgerPort,
    InProcessExperienceLedger,
)
from jshi.evaluation import EvaluationEvent, InProcessEvaluationSystem, new_id
from jshi.assembly import (
    ActivityWindowSource,
    AssemblyContext,
    AssemblyFragment,
    AssemblySpeaker,
    CurrentStateAssembler,
    IdentitySource,
    MemorySource,
    ObjectSource,
    PersonalWorldSource,
    SourceLoadReport,
)
from jshi.attention import ChancePort, PlaceholderChance
from jshi.core import SubjectState
from jshi.feedback import PlaceholderResultFeedback, ResultFeedbackPort
from jshi.governance import (
    ContinuityCheckPort,
    ContinuityFinding,
    PlaceholderContinuityCheck,
)
from jshi.identity import IdentityRepository
from jshi.intent import IntentPort, PlaceholderIntent
from jshi.memory import (
    InProcessHistoryMemory,
    InProcessMemoryBackend,
    MemoryPort,
    MemoryShell,
    RecallCoordinator,
    RecallEvaluatorPort,
    RecallExecution,
    RecalledFragment,
    RuleBasedRecallEvaluator,
)
from jshi.memorycontrol import InProcessMemoryControl
from jshi.objects import InProcessObjectSystem, ObjectSystemPort
from jshi.models import ModelPort, ModelRequest, ModelSpeaker, ObjectAssessment, ResponsePlan
from jshi.recognition import (
    CarrierEntry,
    MIN_OBJECT_CONFIDENCE,
    ObjectProfile,
    ObjectProfileRepository,
    ObjectRecognitionPort,
    ProfileObjectRecognition,
    SpeakerCandidate,
    normalize_text,
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

if TYPE_CHECKING:
    from jshi.personalworld import PersonalWorldPort


logger = logging.getLogger(__name__)


# 上下文补充策略：当前占位为最多一轮；执行与记忆侧指标归 09。
FOLLOWUP_RECALL_MAX_ROUNDS = 1


def verbal_text(plan: ResponsePlan) -> str:
    """说话文本只来自 respond 下的 verbal item。"""
    return plan.verbal_text()


def should_emit_language_action(plan: ResponsePlan) -> bool:
    """对外说话才落 language_action。think/ignore/wait 不说话。"""
    return bool(plan.verbal_text())


def should_trigger_embodied(plan: ResponsePlan) -> bool:
    """embodied 可与四个 mode 组合。"""
    return plan.has_embodied()


@dataclass(frozen=True)
class AssembledCurrentState:
    """Pre-model working set. Never invents subject-facing open matter."""

    input_text: str
    subject_state: SubjectState
    personal_items: tuple[PersonalItem, ...]
    context_view: ContextViewState
    active_segment_ids: tuple[str, ...]
    recalled: tuple[RecalledFragment, ...]
    fragments: tuple[AssemblyFragment, ...] = ()
    source_report: tuple[SourceLoadReport, ...] = ()
    speaker: AssemblySpeaker | None = None

    @property
    def active_event_ids(self) -> tuple[str, ...]:
        """兼容旧名称：实际是活动段 id。"""
        return self.active_segment_ids

    @property
    def active_zone(self) -> ContextViewState:
        return self.context_view


@dataclass(frozen=True)
class SubjectPreview:
    """preview-state 输出：对象解析 + 活跃区装载 + 组装快照（只读，不落库）。"""

    speaker: SpeakerCandidate
    context_view: ContextViewState
    assembled: AssembledCurrentState

    @property
    def active_zone(self) -> ContextViewState:
        return self.context_view


@dataclass(frozen=True)
class SubjectActivityResult:
    activity: Activity
    perception: CognitiveContent
    response_plan: ResponsePlan
    action_text: str
    current_state: AssembledCurrentState
    speaker: SpeakerCandidate = field(
        default_factory=lambda: SpeakerCandidate(subject_id="", actor_object_id="")
    )


class SubjectProcess:
    """最小主体循环：识别→落位→读取上一活跃区→组装→活动→认知(可追加召回)→行动→按方案编活跃区→收尾。

    各系统可独立替换为真实实现，不改主流程顺序。
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
        assembler: CurrentStateAssembler | None = None,
        recall_evaluator: RecallEvaluatorPort | None = None,
        activity_ledger: ExperienceLedgerPort | None = None,
        object_system: ObjectSystemPort | None = None,
    ) -> None:
        self.repository = repository
        self.identities = identities
        self.cognition = cognition
        self.memory = memory or MemoryShell(InProcessMemoryBackend(repository))
        self.recall_coordinator = RecallCoordinator(repository, self.memory)
        self.recall_evaluator = recall_evaluator or RuleBasedRecallEvaluator()
        self.activity_ledger = activity_ledger or InProcessExperienceLedger()
        self.activity_close = InProcessActivityClose(repository)
        self.response_mark = InProcessResponseMark(repository)
        self.action_router = InProcessActionRouter(
            repository,
            PlaceholderRobotAction(),
            PlaceholderSpeech(),
        )
        self.evaluation = InProcessEvaluationSystem()
        self.memory_control = InProcessMemoryControl(
            self.activity_ledger,
            self.memory,
        )
        if personal_world is None:
            from jshi.personalworld import InProcessPersonalWorld

            personal_world = InProcessPersonalWorld(repository)
        self.personal_world = personal_world
        self.profiles = ObjectProfileRepository(repository.path)
        self.object_system = object_system or InProcessObjectSystem(self.profiles)
        self.recognition = recognition or ProfileObjectRecognition(
            self.profiles, memory_matcher=self._match_object_by_memory
        )
        self.active_zone = active_zone or InProcessActiveZone(self.activity_ledger)
        self.intent = intent or PlaceholderIntent()
        self.feedback = feedback or PlaceholderResultFeedback()
        self.governance = governance or PlaceholderContinuityCheck()
        self.chance = chance or PlaceholderChance()
        self.assembler = assembler or CurrentStateAssembler(
            sources=(
                IdentitySource(self.identities),
                ObjectSource(),
                ActivityWindowSource(),
                PersonalWorldSource(self.personal_world),
                MemorySource(
                    repository, memory=self.memory, profiles=self.profiles
                ),
            ),
            chance=self.chance,
        )
        self.reflection = reflection or PlaceholderReflection(self)

    # ------------------------------------------------------------------
    # 阶段②/③ 前置：活跃区装载与单一路径组装
    # ------------------------------------------------------------------

    def assemble_current_state(
        self,
        subject_id: str,
        input_text: str,
        view: ContextViewState | None = None,
        *,
        speaker: SpeakerCandidate | None = None,
        object_id: str | None = None,
    ) -> AssembledCurrentState:
        """单一路径组装（只读已有记录）：
        委托组装器收集各装载源，源内去重、生成快照与报告；
        不调用模型、不写长期记录、不再解析对象。
        """
        view = view or self.active_zone.load(subject_id)
        assembly_speaker = self._assembly_speaker(speaker, object_id)
        ctx = AssemblyContext(
            subject_id=subject_id,
            input_text=input_text,
            speaker=assembly_speaker,
            context_view=view,
            recall_level=1,
        )
        working_set = self.assembler.assemble(ctx)
        return AssembledCurrentState(
            input_text=input_text,
            speaker=working_set.speaker,
            subject_state=working_set.subject_state,
            personal_items=tuple(working_set.personal_items),
            context_view=view,
            active_segment_ids=tuple(view.segment_refs),
            recalled=(),
            fragments=working_set.fragments,
            source_report=working_set.report,
        )

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
        view = self.active_zone.load(subject_id)
        assembled = self.assemble_current_state(
            subject_id, input_text, view, speaker=speaker
        )
        return SubjectPreview(speaker=speaker, context_view=view, assembled=assembled)

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
        normalized = normalize_text(
            text,
            speaker_id=speaker.object_id,
            subject_id=subject_id,
            speaker_names=(speaker.label, *speaker.aliases),
            mentioned=self._mentioned_names(speaker.mentioned_object_ids),
        )
        fact = HistoryRecord(
            subject_id=subject_id,
            kind=HistoryKind.FACT,
            event_type="external_input",
            content={
                "text": text,
                "normalized_text": normalized,
                "source": speaker.label,
                "object_id": speaker.object_id,
                "object_status": speaker.status,
                "object_confidence": speaker.confidence,
                "object_ref": object_ref,
                "channel": channel,
            },
        )
        self.repository.add_history(fact)
        self.activity_ledger.append_external(
            subject_id,
            actor_object_id=speaker.actor_object_id,
            text_raw=text,
            text_normalized=normalized,
            source_ids=(fact.id,),
            mentioned_object_ids=speaker.mentioned_object_ids,
        )

        # 新对象落库（通过门禁后）：暂定档案，来源 = 输入事实 id
        if self.profiles.get(speaker.object_id) is None:
            self.object_system.ensure_provisional(
                object_id=speaker.object_id,
                label=speaker.label,
                source=fact.id,
                carriers=speaker.carriers,
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

        # 阶段② 读取既往上下文：只读 16 当前 ContextViewState
        view = self.active_zone.load(subject_id)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="context_window_loaded",
                content={
                    "input": text,
                    "version": view.version,
                    "segment_ids": list(view.segment_refs),
                    "last_applied_sequence": view.last_applied_sequence,
                },
                source_ids=(fact.id, *view.segment_refs),
            )
        )

        # 阶段③ 当前状态组装（单一路径，只读已有记录）
        current = self.assemble_current_state(
            subject_id, text, view, speaker=speaker
        )
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="current_state_assembled",
                content={
                    "input": text,
                    "segment_ids": list(current.active_segment_ids),
                    "version": view.version,
                    "object_id": speaker.object_id,
                    "label": speaker.label,
                    "value_count": len(current.subject_state.salient_values),
                    "commitment_count": len(current.subject_state.commitments),
                    "recalled_event_ids": [
                        item.event_id for item in current.recalled
                    ],
                    "sources": [
                        {
                            "source": report.source,
                            "status": report.status,
                            "count": len(report.loaded_ids),
                            "budget": report.budget,
                            "ids": list(report.loaded_ids),
                            "skipped": list(report.skipped_ids),
                            "error": report.error,
                        }
                        for report in current.source_report
                    ],
                },
                source_ids=(fact.id, *current.active_segment_ids),
            )
        )

        # 阶段④ 活动建立
        activity = Activity(
            subject_id=subject_id,
            kind=ActivityKind.EXTERNAL,
            trigger=fact.id,
        )
        self.repository.add_activity(activity)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="activity_created",
                content={
                    "activity_id": activity.id,
                    "kind": activity.kind.value,
                    "trigger": activity.trigger,
                    "status": activity.status.value,
                },
                source_ids=(fact.id,),
            )
        )
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

        # 阶段⑤ 认知活动（可追加召回）
        response, working_recalled = self._cognize(
            subject_id, activity, current, perception
        )
        activity, final_statuses, _ = self.mark_activity_response_status(
            subject_id,
            activity,
            response.response_plan,
        )
        self.evaluation.emit(
            EvaluationEvent(
                event_id=f"eval-response-{activity.id}",
                subject_id=subject_id,
                activity_id=activity.id,
                event_type="activity_response_marked",
                payload={"response_statuses": list(final_statuses)},
                source_ids=(activity.id,),
            )
        )
        if response.object_assessment is not None:
            speaker = self._apply_object_assessment(
                subject_id, speaker, fact, activity.id, response.object_assessment
            )

        # 阶段⑥：10 按 item 分发。verbal 落记录并走独立语音占位；embodied 走肢体占位。
        # think/ignore/wait 不说话、不发音，活动仍结束。结果反馈与语言同级。
        plan = response.response_plan
        spoken_text = verbal_text(plan)
        spoke = bool(spoken_text)
        embodied = should_trigger_embodied(plan)
        action_id = ""
        if spoke or embodied:
            action_result = self.action_router.dispatch(
                subject_id=subject_id,
                activity_id=activity.id,
                action_text=spoken_text,
                model=response.model,
                source_id=activity.id,
                response_plan=plan,
            )
            action_id = action_result.action_id
            self.evaluation.emit(
                EvaluationEvent(
                    event_id=f"eval-action-{activity.id}",
                    subject_id=subject_id,
                    activity_id=activity.id,
                    event_type="action_dispatched",
                    payload={
                        "action_id": action_id,
                        "spoke": spoke,
                        "speech_triggered": action_result.speech_triggered,
                        "robot_action_triggered": action_result.robot_action_triggered,
                    },
                    source_ids=(activity.id, *(item for item in (action_id,) if item)),
                )
            )
            self.feedback.ingest_result(
                subject_id,
                activity.id,
                spoken_text
                or next(
                    (item.text for item in plan.items if item.channel == "embodied"),
                    plan.reason,
                ),
            )
        plan_payload = {
            "mode": plan.mode,
            "reason": plan.reason,
            "items": [
                {"channel": item.channel, "text": item.text} for item in plan.items
            ],
        }
        if spoke:
            self.activity_ledger.append_subject_reply(
                subject_id,
                text_raw=spoken_text,
                text_normalized=normalize_text(
                    spoken_text,
                    speaker_id=subject_id,
                    subject_id=subject_id,
                ),
                source_ids=(activity.id, *(item for item in (action_id,) if item)),
                response_plan=plan_payload,
                response_statuses=final_statuses,
            )
        else:
            self.activity_ledger.append_subject_state(
                subject_id,
                state_delta={
                    "mode": plan.mode,
                    "reason": plan.reason,
                    "embodied": [
                        item.text for item in plan.items if item.channel == "embodied"
                    ],
                },
                source_ids=(activity.id,),
                response_plan=plan_payload,
                response_statuses=final_statuses,
            )
        self.activity_ledger.apply_context_assessment(
            subject_id,
            getattr(response, "context_assessment", None),
            allow_edit=True,
            recall_excerpts=tuple(
                (f"memory:{item.event_id}", item.text) for item in working_recalled
            ),
            speaker_object_id=(
                speaker.object_id if speaker is not None else None
            ),
            protected_refs=tuple(
                fragment.id
                for fragment in current.fragments
                if fragment.always or fragment.source in {"object", "identity"}
            ),
        )

        # 阶段⑦ 收尾：关闭本活动。30 失败不影响完成。
        close_result = self.activity_close.close(
            subject_id,
            activity.id,
            final_response_statuses=final_statuses,
            action_id=action_id,
            reason=(
                "external_activity_finished_after_action"
                if spoke
                else f"external_activity_finished_mode_{response.response_plan.mode}"
            ),
        )
        completed = self.repository.get_activity(close_result.activity_id)
        if close_result.handoff_to_memory_control:
            memory_result = self.memory_control.run_once(subject_id)
            self.evaluation.emit(
                EvaluationEvent(
                    event_id=f"eval-memory-{activity.id}",
                    subject_id=subject_id,
                    activity_id=activity.id,
                    event_type="memory_control_attempted",
                    payload={
                        "status": memory_result.status,
                        "ingest_id": memory_result.ingest_id,
                        "error": memory_result.error,
                    },
                    source_ids=(activity.id,),
                )
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
            plan,
            spoken_text,
            current,
            speaker=speaker,
        )

    def _cognize_once(
        self,
        current: AssembledCurrentState,
        working_recalled: Sequence[RecalledFragment],
    ) -> object:
        """05 认知层：一次模型调用，只产出回应与上下文方案，不执行记忆。"""
        speaker = None
        if current.speaker is not None:
            speaker = ModelSpeaker(
                object_id=current.speaker.object_id,
                label=current.speaker.label,
                aliases=current.speaker.aliases,
                status=current.speaker.status,
            )
        return self.cognition.generate(
            ModelRequest(
                purpose="subject_activity",
                input_text=current.input_text,
                subject_state=current.subject_state,
                speaker=speaker,
                context=self._model_context(
                    working_recalled,
                    current.fragments,
                    current.context_view,
                ),
            )
        )

    def _cognize(
        self,
        subject_id: str,
        activity: Activity,
        current: AssembledCurrentState,
        perception: CognitiveContent,
    ) -> tuple[object, list[RecalledFragment]]:
        """主流程认知编排：05 产出方案，09 协调器执行记忆补充，再继续认知。"""
        working_recalled: list[RecalledFragment] = list(current.recalled)
        known_ids = {item.event_id for item in working_recalled}
        response = self._cognize_once(current, working_recalled)
        rounds = 0
        executions: list[RecallExecution] = []

        while (
            response.recall_requests
            and rounds < FOLLOWUP_RECALL_MAX_ROUNDS
        ):
            rounds += 1
            execution = self.recall_coordinator.execute_round(
                subject_id=subject_id,
                activity_id=activity.id,
                perception_id=perception.id,
                round_number=rounds,
                requests=response.recall_requests,
                known_ids=known_ids,
            )
            executions.append(execution)
            working_recalled.extend(execution.fresh)
            self.evaluation.emit(
                EvaluationEvent(
                    event_id=new_id(),
                    subject_id=subject_id,
                    activity_id=activity.id,
                    event_type="recall_executed",
                    payload={
                        "round": rounds,
                        "returned_count": execution.returned_count,
                        "fresh_ids": list(execution.fresh_ids),
                    },
                    source_ids=execution.fresh_ids,
                )
            )
            logger.info(
                "recall_round=%s query=%r returned=%s fresh=%s duration_ms=%s",
                rounds,
                execution.request["query"],
                execution.returned_count,
                len(execution.fresh_ids),
                execution.duration_ms,
            )
            response = self._cognize_once(current, working_recalled)

        truncated = bool(
            executions
            and response.recall_requests
            and rounds >= FOLLOWUP_RECALL_MAX_ROUNDS
        )
        executions = list(
            self.recall_coordinator.commit_metrics(
                subject_id=subject_id,
                activity_id=activity.id,
                executions=executions,
                truncated=truncated,
            )
        )

        self.evaluation.emit(
            EvaluationEvent(
                event_id=new_id(),
                subject_id=subject_id,
                activity_id=activity.id,
                event_type="response_plan_ready",
                payload={
                    "mode": response.response_plan.mode,
                    "reason": response.response_plan.reason,
                },
                source_ids=(activity.id, perception.id),
            )
        )

        added_ids = tuple(
            event_id
            for entry in executions
            for event_id in entry.fresh_ids
        )
        cited_ids = tuple(
            dict.fromkeys(
                (
                    *(item.event_id for item in working_recalled),
                    *(
                        source_id
                        for fragment in current.fragments
                        for source_id in fragment.source_ids
                    ),
                )
            )
        )
        if added_ids:
            self.recall_coordinator.record_reference(
                subject_id=subject_id,
                activity_id=activity.id,
                added_ids=added_ids,
                cited_ids=cited_ids,
            )
        return response, working_recalled

    def mark_activity_response_status(
        self,
        subject_id: str,
        activity: Activity,
        response_plan: ResponsePlan,
        *,
        human_override: Sequence[str] | None = None,
    ) -> tuple[Activity, tuple[str, ...], tuple[str, ...]]:
        """06：校验并标记 response_plan。不写经历、不改活跃区、不投递记忆。"""
        marked = self.response_mark.mark(
            subject_id,
            activity.id,
            response_plan,
            human_override=human_override,
        )
        updated = self.repository.get_activity(activity.id)
        return updated, marked.final, marked.unknown

    def _apply_object_assessment(
        self,
        subject_id: str,
        speaker: SpeakerCandidate,
        fact: HistoryRecord,
        activity_id: str,
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
                    "activity_id": activity_id,
                },
                source_ids=(activity_id, fact.id),
            )
        )
        updated = speaker
        if assessment.conclusion == "confirm":
            self.object_system.confirm(speaker.object_id)
            updated = replace(
                speaker, status="confirmed", confidence=max(speaker.confidence, 0.85)
            )
        elif assessment.conclusion == "deny":
            self.object_system.deny(speaker.object_id)
            updated = replace(speaker, status="rejected", confidence=0.0)
        return updated

    def _mentioned_names(
        self,
        mentioned_object_ids: tuple[str, ...],
    ) -> dict[str, str]:
        """把上游传入的提及对象 id 解析为「名字 / 称呼 → object_id」，供归一化替换。"""
        mapping: dict[str, str] = {}
        for object_id in mentioned_object_ids:
            profile = self.profiles.get(object_id)
            if profile is None:
                continue
            for name in (profile.label, *profile.aliases):
                if name:
                    mapping[name] = object_id
        return mapping

    def _match_object_by_memory(
        self,
        subject_id: str,
        text: str,
        candidates: tuple[ObjectProfile, ...],
    ) -> tuple[ObjectProfile, float] | None:
        """重名消歧占位：按对象过滤事实历史，词重叠打分；须显著领先才返回。"""
        facts = self.repository.list_history(subject_id, kind=HistoryKind.FACT)
        bigrams = {
            text.lower()[index : index + 2]
            for index in range(max(0, len(text) - 1))
        }
        scored: list[tuple[float, ObjectProfile]] = []
        for candidate in candidates:
            related = [
                record
                for record in facts
                if record.content.get("object_id") == candidate.object_id
            ]
            hay = " ".join(
                str(record.content.get("text", "")) for record in related
            ).lower()
            score = float(sum(1 for gram in bigrams if gram in hay))
            scored.append((score, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best_score <= 0:
            return None
        if second > 0 and best_score < second * 2:
            return None
        return best, best_score

    def _assembly_speaker(
        self,
        speaker: SpeakerCandidate | None,
        object_id: str | None,
    ) -> AssemblySpeaker | None:
        if speaker is not None:
            label = speaker.label
            aliases = speaker.aliases
            status = speaker.status
            if self.profiles is not None:
                profile = self.profiles.get(speaker.object_id)
                if profile is not None:
                    label = label or profile.label
                    aliases = aliases or profile.aliases
                    status = status or profile.status
            return AssemblySpeaker(
                object_id=speaker.object_id,
                label=label,
                aliases=tuple(aliases),
                status=status,
            )
        if not object_id:
            return None
        profile = self.profiles.get(object_id) if self.profiles is not None else None
        if profile is None:
            return AssemblySpeaker(object_id=object_id, label="", aliases=())
        return AssemblySpeaker(
            object_id=object_id,
            label=profile.label,
            aliases=profile.aliases,
            status=profile.status,
        )

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
        recalled: Sequence[RecalledFragment],
        fragments: Sequence[AssemblyFragment],
        context_view: ContextViewState | None = None,
    ) -> tuple[dict[str, object], ...]:
        items: list[dict[str, object]] = [
            {
                "id": fragment.id,
                "kind": fragment.kind,
                "content": fragment.content,
                "status": fragment.status,
                "source": fragment.source,
            }
            for fragment in fragments
        ]
        if context_view is not None:
            items.append(
                {
                    "id": f"context-v{context_view.version}",
                    "kind": "active_zone_refs",
                    "content": "",
                    "status": "active",
                    "source": "activity",
                    "segment_refs": list(context_view.segment_refs),
                    "recall_refs": [ref for ref, _text in context_view.recall_excerpts],
                    "speaker_object_id": context_view.speaker_object_id,
                }
            )
        items.extend(
            {
                "id": f"memory:{item.event_id}",
                "kind": "recalled_fact",
                "content": item.text,
                "status": "active",
                "source": "memory",
                "event_type": item.event_type,
            }
            for item in recalled
        )
        return tuple(items)

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
        )
        self.repository.add_activity(activity)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="activity_created",
                content={
                    "activity_id": activity.id,
                    "kind": activity.kind.value,
                    "trigger": activity.trigger,
                    "status": activity.status.value,
                },
                source_ids=(),
            )
        )
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
        self.activity_close.close(
            subject_id,
            activity.id,
            final_response_statuses=(),
            action_id="",
            reason="internal_activity_finished",
        )
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
        self.activity_ledger.append_subject_state(
            current.subject_id,
            state_delta={
                "cognitive_content_id": content_id,
                "from": transition.from_state,
                "to": transition.to_state,
                "reason": reason,
            },
            source_ids=(transition.id, *source_ids),
        )
        return updated

    def add_personal_item(
        self,
        subject_id: str,
        kind: PersonalKind,
        content: str,
        source_ids: tuple[str, ...] = (),
        importance: float = 1.0,
        *,
        level: str = "中",
        entry_type: str = "",
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
            level=level,
            entry_type=entry_type,
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
