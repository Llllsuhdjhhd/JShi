from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Sequence

from jshi.activezone import (
    ActiveZonePort,
    ActiveZoneView,
    InProcessActiveZone,
)
from jshi.action import InProcessActionRouter, PlaceholderRobotAction
from jshi.activityclose import InProcessActivityClose
from jshi.experienceledger import ExperienceLedgerPort, InProcessExperienceLedger
from jshi.evaluation import EvaluationEvent, InProcessEvaluationSystem, new_id
from jshi.assembly import (
    ActivityWindowSource,
    AssemblyContext,
    AssemblyFragment,
    CurrentStateAssembler,
    EpistemicSource,
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
from jshi.models import ModelPort, ModelRequest, ObjectAssessment, ResponsePlan
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

if TYPE_CHECKING:
    from jshi.personalworld import PersonalWorldPort


logger = logging.getLogger(__name__)


# 上下文补充策略：当前占位为最多一轮；执行与记忆侧指标归 09。
FOLLOWUP_RECALL_MAX_ROUNDS = 1
RESPONSE_STATUSES = frozenset(
    {"respond", "verbal", "embodied", "think", "ignore", "wait"}
)
SILENT_MODES = frozenset({"think", "ignore", "wait"})


def should_emit_language_action(plan: ResponsePlan) -> bool:
    """对外说话才落 language_action。think/ignore/wait 都结束本活动，但不说话。"""
    if plan.mode in SILENT_MODES:
        return False
    return any(
        item.channel == "verbal" and item.text.strip() for item in plan.items
    )


@dataclass(frozen=True)
class AssembledCurrentState:
    """Pre-model working set. Never invents subject-facing open matter."""

    input_text: str
    subject_state: SubjectState
    personal_items: tuple[PersonalItem, ...]
    active_zone: ActiveZoneView
    active_segment_ids: tuple[str, ...]
    recalled: tuple[RecalledFragment, ...]
    fragments: tuple[AssemblyFragment, ...] = ()
    source_report: tuple[SourceLoadReport, ...] = ()

    @property
    def active_event_ids(self) -> tuple[str, ...]:
        """兼容旧名称：实际是活动段 id。"""
        return self.active_segment_ids


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
    speaker: SpeakerCandidate = field(
        default_factory=lambda: SpeakerCandidate(subject_id="", actor_object_id="")
    )


class SubjectProcess:
    """最小主体循环：识别→落位→活跃区装载→组装→活动→认知(可追加召回)→行动→收尾。

    每个系统都是占位接口：身份识别、活跃区、意图、受约束偶然性、结果反馈、
    治理检验、反思。各系统可独立替换为真实实现，不改主流程顺序。
    归属判断已废弃；事件与记忆侧边界归 07/09，不在主流程活动窗口处理。
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
        self.action_router = InProcessActionRouter(
            repository,
            PlaceholderRobotAction(),
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
                ActivityWindowSource(),
                PersonalWorldSource(self.personal_world),
                MemorySource(repository, memory=self.memory),
                EpistemicSource(),
                ObjectSource(),
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
        view: ActiveZoneView | None = None,
        *,
        object_id: str | None = None,
    ) -> AssembledCurrentState:
        """单一路径组装（只读已有记录）：
        委托组装器收集各装载源，合并去重、预算截断、生成快照与报告；
        不调用模型、不写长期记录。
        """
        view = view or self.active_zone.load(subject_id, input_text)
        ctx = AssemblyContext(
            subject_id=subject_id,
            input_text=input_text,
            object_id=object_id,
            active_zone=view,
            budget_extra=4,
            recall_level=1,
        )
        working_set = self.assembler.assemble(ctx)
        return AssembledCurrentState(
            input_text=input_text,
            subject_state=working_set.subject_state,
            personal_items=tuple(working_set.personal_items),
            active_zone=view,
            active_segment_ids=working_set.active_segment_ids,
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
        view = self.active_zone.load(subject_id, input_text)
        assembled = self.assemble_current_state(
            subject_id, input_text, view, object_id=speaker.object_id
        )
        return SubjectPreview(speaker=speaker, active_zone=view, assembled=assembled)

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
        self.activity_ledger.append_external(
            subject_id,
            actor_object_id=speaker.actor_object_id,
            text_raw=text,
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

        # 阶段② 活跃区装载：只取原始活动窗口，不识别事件
        view = self.active_zone.load(subject_id, text)
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="context_window_loaded",
                content={
                    "input": text,
                    "segment_ids": [segment.segment_id for segment in view.segments],
                    "start_sequence": view.start_sequence,
                    "budget_chars": view.budget_chars,
                },
                source_ids=(
                    fact.id,
                    *(segment.segment_id for segment in view.segments),
                ),
            )
        )

        # 阶段③ 当前状态组装（单一路径，只读已有记录）
        current = self.assemble_current_state(
            subject_id, text, view, object_id=speaker.object_id
        )
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="current_state_assembled",
                content={
                    "input": text,
                    "segment_ids": list(current.active_segment_ids),
                    "budget_chars": view.budget_chars,
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

        # 阶段④ 活动建立（事件挂载在认知后由模型聚焦回填）
        activity = Activity(
            subject_id=subject_id,
            kind=ActivityKind.EXTERNAL,
            trigger=fact.id,
            active_concern_ids=(),
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

        # 阶段⑤ 认知活动（多轮，可追加召回；模型聚焦事件）
        thought, response = self._cognize(
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
                subject_id, speaker, fact, thought, response.object_assessment
            )

        # 阶段⑥ 行动：仅 verbal 落语言行动。think/ignore/wait 不说话，活动仍结束。
        # wait ≠ think：wait 是本轮等后续输入或外部结果；think 是本轮不对外说。
        # 下一轮外部输入创建新活动，不把同一 Activity 挂起。embodied 仍占位。
        action_id = ""
        spoke = should_emit_language_action(response.response_plan)
        if spoke:
            action_result = self.action_router.dispatch(
                subject_id=subject_id,
                activity_id=activity.id,
                action_text=thought.content,
                model=thought.model,
                source_id=thought.id,
                response_plan=response.response_plan,
            )
            action_id = action_result.action_id
            self.evaluation.emit(
                EvaluationEvent(
                    event_id=f"eval-action-{action_id}",
                    subject_id=subject_id,
                    activity_id=activity.id,
                    event_type="language_action_recorded",
                    payload={
                        "action_id": action_id,
                        "robot_action_triggered": action_result.robot_action_triggered,
                    },
                    source_ids=(action_id,),
                )
            )
            self.activity_ledger.append_subject_reply(
                subject_id,
                text_raw=thought.content,
                source_ids=(thought.id, action_id),
                response_statuses=final_statuses,
            )
            self.feedback.ingest_result(subject_id, activity.id, thought.content)
        else:
            self.activity_ledger.append_subject_state(
                subject_id,
                state_delta={
                    "mode": response.response_plan.mode,
                    "reason": response.response_plan.reason,
                },
                source_ids=(thought.id,),
                response_statuses=final_statuses,
            )

        # 阶段⑦ 收尾：推进活跃区窗口起点，关闭本活动。30 失败不影响完成。
        active_through = self.activity_ledger.head_sequence(subject_id)
        if active_through:
            self.active_zone.advance(subject_id, active_through)
        close_result = self.activity_close.close(
            subject_id,
            activity.id,
            final_response_statuses=final_statuses,
            thought_id=thought.id,
            action_id=action_id,
            reason=(
                "external_activity_finished_after_action"
                if spoke
                else f"external_activity_finished_mode_{response.response_plan.mode}"
            ),
        )
        completed = self.repository.get_activity(close_result.activity_id)
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
            thought,
            thought.content if spoke else "",
            current,
            speaker=speaker,
        )

    def _cognize_once(
        self,
        current: AssembledCurrentState,
        working_recalled: Sequence[RecalledFragment],
    ) -> object:
        """05 认知层：一次模型调用，只产出回应与上下文候选，不执行记忆。"""
        return self.cognition.generate(
            ModelRequest(
                purpose="subject_activity",
                input_text=current.input_text,
                subject_state=current.subject_state,
                context=self._model_context(
                    working_recalled,
                    current.fragments,
                ),
            )
        )

    def _materialize_thought(
        self,
        subject_id: str,
        activity: Activity,
        current: AssembledCurrentState,
        perception: CognitiveContent,
        working_recalled: Sequence[RecalledFragment],
        response: object,
    ) -> CognitiveContent:
        thought = CognitiveContent(
            subject_id=subject_id,
            activity_id=activity.id,
            kind=CognitiveKind.INFERENCE,
            content=response.text,
            epistemic_status=EpistemicStatus.CONSIDERING,
            evidence_kind=EvidenceKind.COGNITIVE_REASONING,
            source_ids=(
                perception.id,
                *(
                    source_id
                    for fragment in current.fragments
                    for source_id in fragment.source_ids
                ),
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
        return thought

    def _cognize(
        self,
        subject_id: str,
        activity: Activity,
        current: AssembledCurrentState,
        perception: CognitiveContent,
    ) -> tuple[CognitiveContent, object]:
        """主流程认知编排：05 产出候选，09 协调器执行记忆补充，再继续认知。"""
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

        thought = self._materialize_thought(
            subject_id,
            activity,
            current,
            perception,
            working_recalled,
            response,
        )
        self.evaluation.emit(
            EvaluationEvent(
                event_id=new_id(),
                subject_id=subject_id,
                activity_id=activity.id,
                event_type="thought_materialized",
                payload={"cognitive_content_id": thought.id},
                source_ids=(thought.id,),
            )
        )

        added_ids = tuple(
            event_id
            for entry in executions
            for event_id in entry.fresh_ids
        )
        if added_ids:
            self.recall_coordinator.record_reference(
                subject_id=subject_id,
                activity_id=activity.id,
                added_ids=added_ids,
                thought_source_ids=thought.source_ids,
            )
        return thought, response

    def mark_activity_response_status(
        self,
        subject_id: str,
        activity: Activity,
        response_plan: ResponsePlan,
        *,
        human_override: Sequence[str] | None = None,
    ) -> tuple[Activity, tuple[str, ...], tuple[str, ...]]:
        """06：校验模型推荐的回复状态并标记到活动。"""
        recommended = tuple(
            dict.fromkeys(
                [
                    response_plan.mode,
                    *(item.channel for item in response_plan.items),
                ]
            )
        )
        valid = tuple(status for status in recommended if status in RESPONSE_STATUSES)
        unknown = tuple(status for status in recommended if status not in RESPONSE_STATUSES)
        final = (
            tuple(dict.fromkeys(human_override))
            if human_override is not None
            else valid
        )
        updated = self.repository.update_activity(
            activity.id,
            response_statuses=final,
        )
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="activity_response_state",
                content={
                    "activity_id": updated.id,
                    "recommended": list(recommended),
                    "final": list(final),
                    "unknown_statuses": list(unknown),
                    "reason": response_plan.reason,
                    "items": [
                        {"channel": item.channel, "text": item.text}
                        for item in response_plan.items
                    ],
                    "source": "human_overridden" if human_override is not None else "model_recommended",
                },
                source_ids=(updated.id,),
            )
        )
        return updated, final, unknown

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
            self.object_system.confirm(speaker.object_id)
            updated = replace(
                speaker, status="confirmed", confidence=max(speaker.confidence, 0.85)
            )
        elif assessment.conclusion == "deny":
            self.object_system.deny(speaker.object_id)
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
        recalled: Sequence[RecalledFragment],
        fragments: Sequence[AssemblyFragment],
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "id": fragment.id,
                "kind": fragment.kind,
                "content": fragment.content,
                "status": fragment.status,
                "source": fragment.source,
            }
            for fragment in fragments
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
            active_concern_ids=(),
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
        self.repository.update_activity(
            activity.id,
            status=ActivityStatus.COMPLETED,
            reason="internal_activity_finished",
        )
        self.repository.add_history(
            HistoryRecord(
                subject_id=subject_id,
                kind=HistoryKind.SUBJECT,
                event_type="activity_completed",
                content={
                    "activity_id": activity.id,
                    "to": ActivityStatus.COMPLETED.value,
                    "reason": "internal_activity_finished",
                },
                source_ids=(),
            )
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
