from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Mapping, Sequence
from jshi.core.envelope import InputEnvelope

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
    ManualInputSource,
    ObjectSource,
    PersonExperienceSource,
    PersonPortraitSource,
    PersonalWorldSource,
    SourceLoadReport,
    ToolSource,
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
from jshi.effectiveness import EffectivenessPort, InProcessEffectiveness
from jshi.effectiveness.step_inputs import StepInputStore
from jshi.effectiveness.timings import append_timing
from jshi.memory import (
    InProcessMemoryBackend,
    MemoryPort,
    MemoryShell,
    RecallCoordinator,
    RecallEvaluatorPort,
    RecalledFragment,
    RecallStrategyStore,
    RuleBasedRecallEvaluator,
)
from jshi.memorycontrol import InProcessMemoryControl
from jshi.longtermexperience import LongTermExperiencePort
from jshi.objects import InProcessObjectSystem, ObjectSystemPort
from jshi.models import (
    ModelPort,
    ModelRequest,
    ModelResponse,
    ModelSpeaker,
    ObjectAssessment,
    RecallRequest,
    ResponsePlan,
    format_speech_with_action,
    format_unsaid_zone_block,
    format_turn_input,
    STIMULUS_IDLE,
    STIMULUS_SPEECH,
)
from jshi.recognition import (
    CarrierEntry,
    MIN_OBJECT_CONFIDENCE,
    ObjectProfile,
    ObjectProfileRepository,
    ObjectRecognitionPort,
    ProfileObjectRecognition,
    SpeakerCandidate,
)
from jshi.reflection import (
    MOMENT_AFTER_ACTIVITY,
    InProcessReflection,
    IntrospectionRequest,
    IntrospectionRun,
    PlaceholderReflection,
    ReflectionPort,
)
from jshi.privilege import PromptRuleStore
from jshi.style import (
    StylePackStore,
    ZoneBlock,
    ZoneStore,
    boot_instruction_for,
    boot_schema_for,
    format_zone_budget_note,
    is_first_style_turn,
    is_persona,
    reply_instruction_for,
    reply_schema_for,
    value_narration_chars_for,
    write_instruction_for,
    write_schema_for,
    zone_chars_for,
)
from jshi.memory.traces import JsonlRecallTraceStore, RecallTrace
from jshi.tool import (
    StubEngine,
    ToolModule,
    ToolRunner,
    ToolService,
)
from jshi.tool.hang import HangStore

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
    utc_now,
)
from .repository import SubjectRepository

if TYPE_CHECKING:
    from jshi.personalworld import PersonalWorldPort


logger = logging.getLogger(__name__)


def verbal_text(plan: ResponsePlan) -> str:
    """说话文本只来自 respond 下的 verbal item。"""
    return plan.verbal_text()


def should_emit_language_action(plan: ResponsePlan) -> bool:
    """对外说话才落 language_action。think/ignore/wait 不说话。"""
    return bool(plan.verbal_text())


def should_trigger_embodied(plan: ResponsePlan) -> bool:
    """embodied 可与四个 mode 组合。"""
    return plan.has_embodied()


def _speaker_object_mapping(
    speaker: SpeakerCandidate,
    supplied: Mapping[str, str] | None,
    extra_aliases: Sequence[str] = (),
) -> dict[str, str]:
    """信封映射表 + 本轮说话人的名字/称呼 → 档案 id。说话人名字以 01 为准。"""
    mapping = {
        str(key).strip(): str(value).strip()
        for key, value in dict(supplied or {}).items()
        if str(key).strip() and str(value).strip()
    }
    oid = (speaker.object_id or "").strip()
    if not oid:
        return mapping
    for name in (speaker.label, *speaker.aliases, *extra_aliases):
        key = str(name).strip()
        if key:
            mapping[key] = oid
    return mapping


def _mentioned_with_speaker(speaker: SpeakerCandidate) -> tuple[str, ...]:
    oid = (speaker.object_id or "").strip()
    return tuple(
        dict.fromkeys(item for item in (*speaker.mentioned_object_ids, oid) if item)
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


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
    tool_input: str = ""
    tool_hot_state: str = ""
    stimulus: str = STIMULUS_SPEECH
    transport_context: str = ""
    audio_delivery: bool = False
    input_review_text: str = ""
    object_codes: tuple[tuple[str, str], ...] = ()
    tool_codes: tuple[tuple[str, str], ...] = ()
    processing_modes: tuple[str, ...] = ("interaction",)
    input_items: tuple[Mapping[str, object], ...] = ()
    input_parts: tuple[Mapping[str, object], ...] = ()
    pending_write_text: str = ""
    pending_write_ids: tuple[str, ...] = ()
    pending_write_blocks: tuple[str, ...] = ()
    pending_through_sequence: int = 0
    prompt_level: int = 1
    recall_memory: bool = True
    prompt_modules: tuple[str, ...] = ()
    interaction_scene: str = ""


@dataclass(frozen=True)
class SubjectPreview:
    """preview-state 输出：对象解析 + 活跃区装载 + 组装快照（只读，不落库）。"""

    speaker: SpeakerCandidate
    context_view: ContextViewState
    assembled: AssembledCurrentState


@dataclass(frozen=True)
class ActivityTiming:
    """一轮活动各步耗时。旁路观测，不进经历、不进模型。"""

    activity_id: str
    started_at: str
    steps: tuple[tuple[str, float], ...]
    total_ms: float


class StepClock:
    def __init__(self) -> None:
        self.started_at = utc_now()
        self._last = time.perf_counter()
        self.steps: list[tuple[str, float]] = []

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.steps.append((name, round((now - self._last) * 1000, 1)))
        self._last = now

    def finish(self, activity_id: str) -> ActivityTiming:
        return ActivityTiming(
            activity_id=activity_id,
            started_at=self.started_at.isoformat(),
            steps=tuple(self.steps),
            total_ms=round(sum(ms for _name, ms in self.steps), 1),
        )


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
    timing: ActivityTiming | None = None
    selected_tool_ids: tuple[str, ...] = ()


def _plain_turn_input(text: str) -> str:
    """写入记忆和片场的输入原话，不含入口的 JEV 初判。"""
    return "\n".join(
        line for line in (text or "").splitlines() if not line.startswith("JEV 初判")
    ).strip()


class SubjectProcess:
    """最小主体循环：识别→落位→读取上一活跃区→组装→活动→认知（一次）→行动→按方案编活跃区→收尾。

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
        prompt_rules: PromptRuleStore | None = None,
        recall_strategy: RecallStrategyStore | None = None,
        effectiveness: EffectivenessPort | None = None,
        style_packs: StylePackStore | None = None,
        recall_traces: JsonlRecallTraceStore | None = None,
        zone_store: ZoneStore | None = None,
        write_zone: ModelPort | None = None,
        hang_store: HangStore | None = None,
        tool_runner: ToolRunner | None = None,
        tool_service: ToolService | None = None,
        introspection_model: ModelPort | None = None,
        long_term_experience: LongTermExperiencePort | None = None,
        person_review_model: ModelPort | None = None,
    ) -> None:
        self.repository = repository
        self.identities = identities
        self.cognition = cognition
        self.person_review_model = person_review_model or (cognition if hasattr(cognition, '_skill') else None)
        # 写场（②）独立端口：注入时走两调用；未注入退回单调用（保旧契约可跑）。
        self.write_zone = write_zone
        self._write_lock = threading.RLock()
        self._deferred_write = None
        self._scene_writer_lock = threading.Lock()
        self._write_payloads = {}
        self._latest_write_payloads = {}
        self._scene_wakeup = threading.Event()
        self._scene_subjects = set()
        self._scene_thread = None
        self._unknown_inputs = None
        from .pending_scene import PendingScene
        self.pending_scene = PendingScene(repository.path.parent / "pending_scene.json")
        self._voice_party_ids: tuple[str, ...] = ()
        self._code_restore: Callable[[str], str] | None = None
        data_dir = repository.path.parent
        self.step_inputs = StepInputStore(data_dir / "step_inputs.jsonl")
        self._timings_path = data_dir / "activity_timings.jsonl"
        self._write_timings_path = data_dir / "write_timings.jsonl"
        self._write_timing_lock = threading.Lock()
        if tool_service is not None:
            self.tool_service = tool_service
        else:
            store = hang_store or HangStore(data_dir / "hang.jsonl")
            runner = tool_runner or ToolRunner(ToolModule(StubEngine()), store)
            self.tool_service = ToolService(
                hang_store=store,
                runner=runner,
                intake_path=data_dir / "tool.jsonl",
            )
        self.hang_store = self.tool_service.hang_store
        self.tool_runner = self.tool_service.runner
        self.last_activity_timing: ActivityTiming | None = None
        self.last_model_response = None
        self.last_write_response = None
        self.last_boot: str = "否"
        self.last_memory_control = None
        # 本轮组装真正装上的记挂 id（写场后回写用），每轮重置。
        self._turn_tool_ids: tuple[str, ...] = ()
        self._segment_short_map: dict[str, str] = {}
        self._memory_short_map: dict[str, str] = {}
        self.memory = memory or MemoryShell(InProcessMemoryBackend(repository))
        self.long_term_experience = long_term_experience
        self.recall_coordinator = RecallCoordinator(repository, self.memory)
        self.recall_evaluator = recall_evaluator or RuleBasedRecallEvaluator()
        self.activity_ledger = activity_ledger or InProcessExperienceLedger()
        self.prompt_rules = prompt_rules
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
            subject_name=self._subject_display_name,
        )
        if personal_world is None:
            from jshi.personalworld import InProcessPersonalWorld

            personal_world = InProcessPersonalWorld(repository)
        self.personal_world = personal_world
        self.values = getattr(personal_world, "values", None)
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
                *(
                    (PersonExperienceSource(self.long_term_experience),)
                    if self.long_term_experience is not None
                    else ()
                ),
                PersonPortraitSource(self.memory),
                MemorySource(
                    repository, memory=self.memory, profiles=self.profiles
                ),
                ManualInputSource(data_dir / "unknown_inputs.sqlite3"),
                ToolSource(self.tool_service),
            ),
            chance=self.chance,
        )
        self.recall_strategy = recall_strategy or RecallStrategyStore()
        self.effectiveness = effectiveness or InProcessEffectiveness(
            strategy=self.recall_strategy
        )
        self.style_packs = style_packs or StylePackStore()
        self.recall_traces = recall_traces or JsonlRecallTraceStore()
        self.zone_store = zone_store or ZoneStore()
        self._bind_tool_scene_loader()
        self.reflection: ReflectionPort = reflection or InProcessReflection(
            self, model=introspection_model or cognition
        )

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
        stimulus: str = STIMULUS_SPEECH,
        prompt_profile=None,
    ) -> AssembledCurrentState:
        """单一路径组装（只读已有记录）：
        委托组装器收集各装载源，源内去重、生成快照与报告；
        不调用模型、不写长期记录、不再解析对象。
        """
        view = view or self.active_zone.load(subject_id)
        from jshi.core.prompt_profile import PromptProfile
        profile = prompt_profile or PromptProfile()
        assembly_speaker = self._assembly_speaker(speaker, object_id)
        strategy = self.recall_strategy.get(subject_id)
        gap = (self.effectiveness.pending_gap_query(subject_id) or "").strip()
        # 【工具相关】每次组装只选一次：ToolSource 的报告与送进 05 的是同一份。
        speaker_oid = (
            assembly_speaker.object_id if assembly_speaker is not None else ""
        )
        tool_entries: tuple[Mapping[str, object], ...] = ()
        if speaker_oid and profile.includes("tool"):
            tool_entries = self.tool_service.list_tool_related_entries(
                subject_id, speaker_oid, query=input_text
            )
        ctx = AssemblyContext(
            subject_id=subject_id,
            input_text=input_text,
            speaker=assembly_speaker,
            context_view=view,
            recall_level=strategy.default_level,
            recall_limit=strategy.limit,
            extra_queries=(gap,) if gap and profile.recall_memory else (),
            tool_entries=tool_entries,
            recall_enabled=profile.recall_memory,
            portrait_budget_chars={1:800, 2:400, 3:200}[profile.level],
        )
        working_set = self.assembler.assemble(ctx)
        if gap and profile.recall_memory and not any(r.source == 'memory' and r.error for r in working_set.report):
            self.effectiveness.consume_gap(subject_id)
        from jshi.tool.service import format_tool_related

        self._turn_tool_ids = tuple(str(item["id"]) for item in tool_entries)
        tool_codes = tuple((f"T{i}", str(item["id"])) for i, item in enumerate(tool_entries, 1))
        tool_related = format_tool_related(tool_entries, task_codes={oid:code for code,oid in tool_codes})
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
            # 主路径只呈现【工具相关】；裸 tool_input / 热状态标题不再分两路贴。
            tool_input=tool_related,
            tool_codes=tool_codes,
            tool_hot_state="",
            prompt_level=profile.level,
            recall_memory=profile.recall_memory,
            stimulus=stimulus if stimulus == STIMULUS_IDLE else STIMULUS_SPEECH,
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
        objects: Mapping[str, str] | None = None,
        on_reply: Callable[[str], None] | None = None,
        stimulus: str = STIMULUS_SPEECH,
        envelope: InputEnvelope | None = None,
        resolved_speaker: SpeakerCandidate | None = None,
        on_verbal: Callable[[str], None] | None = None,
        on_voice_plan: Callable[[ResponsePlan], None] | None = None,
        defer_write: bool = False,
        code_restore: Callable[[str], str] | None = None,
        action_allowed: Callable[[], bool] | None = None,
        on_conversation_review: Callable | None = None,
        object_codes: Mapping[str, str] | None = None,
        processing_modes: Sequence[str] = ("interaction",),
    ) -> SubjectActivityResult:
        self._deferred_write = None
        self._code_restore = code_restore
        if envelope is not None and (not envelope.final or envelope.text != text.strip()):
            raise ValueError("only final envelopes with matching text enter cognition")
        clock = StepClock()
        self.last_boot = "否"
        self.last_memory_control = None
        self._turn_tool_ids = ()
        idle = stimulus == STIMULUS_IDLE
        if idle:
            text = ""
        # 阶段① 对象必填校验 → 对象解析 → 置信度门禁 → 落位
        if resolved_speaker is None and envelope is not None:
            evidence = envelope.speaker
            profile = self.profiles.get(evidence.object_id)
            resolved_speaker = SpeakerCandidate(subject_id, evidence.object_id, evidence.label,
                confidence=evidence.confidence or 0.0,
                status=profile.status if profile and evidence.status != "unknown" else "provisional",
                reason="voice_unknown" if evidence.status == "unknown" else evidence.method)
        if resolved_speaker is not None:
            if (envelope is None or resolved_speaker.subject_id != subject_id
                    or resolved_speaker.object_id != envelope.speaker.object_id):
                raise ValueError("resolved speaker requires an envelope for this subject")
        else:
            self._require_object_source(object_ref, channel, carriers)
        speaker = resolved_speaker or self.recognition.resolve(
            subject_id, text, object_ref, channel, carriers
        )
        if not speaker.object_id:
            raise ValueError("object candidate must be referenceable (object_id)")
        if envelope is None and speaker.confidence < MIN_OBJECT_CONFIDENCE:
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
        extra_aliases: tuple[str, ...] = ()
        if self.profiles is not None:
            profile = self.profiles.get(speaker.object_id)
            if profile is not None:
                extra_aliases = profile.aliases
        objects = _speaker_object_mapping(speaker, objects, extra_aliases)
        mentioned_object_ids = _mentioned_with_speaker(speaker)
        self._voice_party_ids = ()
        if envelope is not None:
            mentioned_object_ids = tuple(dict.fromkeys((*mentioned_object_ids,
                *(u.speaker.object_id for u in envelope.current_utterances
                  if u.speaker.status in {"introduced", "recognized"}))))
            self._voice_party_ids = tuple(dict.fromkeys(
                u.speaker.object_id for u in envelope.current_utterances
                if u.speaker.object_id and u.speaker.method not in {"unassigned_audio", "voice_scene", "context_attribution"}
                and u.speaker.status in {"introduced", "recognized"}
            ))
        if envelope is not None and envelope.current_utterances:
            # The raw envelope may still carry legacy entry notes or history.
            # Only this batch's original statements become the input fact.
            recorded_text = '\n'.join(f'{u.speaker.label}：{u.text}' for u in envelope.current_utterances)
        else:
            recorded_text = (code_restore(_plain_turn_input(text)) if code_restore else text)
        fact = HistoryRecord(
            subject_id=subject_id,
            kind=HistoryKind.FACT,
            event_type="idle_stimulus" if idle else "external_input",
            content={
                "text": recorded_text,
                **({"envelope": envelope.to_dict()} if envelope else {}),
                "stimulus": STIMULUS_IDLE if idle else STIMULUS_SPEECH,
                "objects": dict(objects),
                "source": "idle" if idle else speaker.label,
                "object_id": speaker.object_id,
                "object_status": speaker.status,
                "object_confidence": speaker.confidence,
                "object_ref": object_ref,
                "channel": channel,
            },
        )
        self.repository.add_history(fact)
        inbound = None
        if not idle:
            inbound = self.activity_ledger.append_external(
                subject_id,
                actor_object_id=speaker.actor_object_id,
                text_raw=recorded_text,
                objects=objects,
                source_ids=(fact.id,),
                mentioned_object_ids=mentioned_object_ids,
            )

        # 新对象落库（通过门禁后）：暂定档案，来源 = 输入事实 id
        if self.profiles.get(speaker.object_id) is None and speaker.reason != "voice_pending" and not (envelope and envelope.speaker.status == "unknown"):
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

        clock.mark("①落位")
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
                    "chars": len(view.context_text or ""),
                    "style_pack_id": view.style_pack_id,
                    "last_applied_sequence": view.last_applied_sequence,
                },
                source_ids=(fact.id, *view.segment_refs),
            )
        )

        clock.mark("②活跃区")
        # 组装只读；清理超时策划与僵尸记挂放在组装之前，由主流程显式做。
        self._tool_reap_stale(subject_id, speaker.object_id)
        # 阶段③ 当前状态组装（单一路径，只读已有记录）
        from jshi.core.prompt_profile import select_profile, PromptProfile
        profile = select_profile((u.input_id for u in envelope.current_utterances), envelope.jev_calls) if envelope is not None else PromptProfile()
        current = self.assemble_current_state(
            subject_id, text, view, speaker=speaker, stimulus=stimulus, prompt_profile=profile
        )
        # A just-recorded input can be returned by the memory source. It is
        # already in input_items, so don't present it as an additional memory.
        current = replace(current, fragments=tuple(fragment for fragment in current.fragments
            if fact.id not in fragment.source_ids))
        from jshi.core.main_input import make_input
        if not idle:
            parts = tuple(asdict_part for asdict_part in (
                {"kind": p.kind, "reference": p.reference, "media_type": p.media_type}
                for p in envelope.parts)) if envelope else ()
            incoming = envelope.current_utterances if envelope else ()
            annotations = {row['input_id']: row for row in envelope.input_annotations} if envelope else {}
            items = tuple(make_input(u.input_id, u.speaker.object_id,
                (dict(object_codes or {}).get(u.speaker.object_id, "") + "（" + u.speaker.label + "）") if object_codes else u.speaker.label, u.text,
                source="text" if "文字输入" in u.identity_note else "audio",
                level="确定" if u.speaker.status == "recognized" or u.speaker.method == "manual_annotation" else (
                    "可能" if u.speaker.method in {"self_report", "context_attribution"} else "不确定"),
                evidence={"self_report": "对方自报称呼，尚未核定", "context_attribution": "入口上下文推测", "voiceprint_match": "入口声纹匹配", "manual_annotation": "人工关联"}.get(u.speaker.method, "入口未确认姓名"),
                parts=parts, number=annotations.get(u.input_id, {}).get('n', ''),
                direction=annotations.get(u.input_id, {}).get('direction', ''),
                relevance=annotations.get(u.input_id, {}).get('relevance', '')) for u in incoming)
            if not items:
                items = (make_input(envelope.input_id if envelope else fact.id, speaker.object_id,
                    speaker.label, text, source=next((p.kind for p in envelope.parts if p.kind != "text"), "text") if envelope else "text",
                    level="确定" if speaker.status == "confirmed" else "不确定",
                    evidence="入口提供的对象归属", parts=parts),)
            current = replace(current, input_items=items, input_parts=parts)
            if envelope and envelope.speaker.status == "unknown" and not incoming:
                self.unknown_inputs.append(envelope.input_id, envelope.speaker.object_id,
                    text, envelope.session_id, envelope.start_ms, envelope.end_ms)
        modes = tuple(dict.fromkeys(str(mode).strip() for mode in processing_modes if str(mode).strip()))
        current = replace(current, processing_modes=modes or ("interaction",))
        if envelope is not None:
            # 信封只留在入口和记录里。主流程看到的是已经整理好的文字。
            current = replace(current, audio_delivery=any(p.kind == "audio" for p in envelope.parts), input_review_text='',
                              interaction_scene=envelope.interaction_scene,
                              object_codes=tuple((object_codes or {}).items()))
            from jshi.core.prompt_profile import select_profile
            profile = select_profile((u.input_id for u in envelope.current_utterances), envelope.jev_calls)
            current = replace(current, prompt_level=profile.level, prompt_modules=profile.modules, recall_memory=profile.recall_memory)
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
                    "tool_codes": dict(current.tool_codes),
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
                            "elapsed_ms": report.elapsed_ms,
                        }
                        for report in current.source_report
                    ],
                },
                source_ids=(fact.id, *current.active_segment_ids),
            )
        )

        clock.mark("③组装")
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
        self._record_recall_trace(
            subject_id,
            activity.id,
            current,
            input_segment_id=inbound.segment_id if inbound is not None else "",
            query=text,
            object_id=speaker.object_id,
        )

        # 感知（已接受的他人报告）
        perception = CognitiveContent(
            subject_id=subject_id,
            activity_id=activity.id,
            kind=CognitiveKind.PERCEPTION,
            content=(
                f"闲时：{speaker.label}仍在场，无新原话"
                if idle
                else f"对方表达：{recorded_text}"
            ),
            epistemic_status=EpistemicStatus.ACCEPTED,
            evidence_kind=EvidenceKind.REPORT,
            source_ids=(fact.id,),
        )
        self.repository.add_cognitive_content(perception)

        clock.mark("④建活动")
        # 这一拍真正装上的记挂 id：组装之后立即快照——写场会再次组装，
        # 不能依赖 self._turn_tool_ids（会被后来的组装覆盖）。
        turn_tool_ids = tuple(getattr(self, "_turn_tool_ids", ()) or ())
        # 阶段④.5：非木头人格首次（无片场）→ boot 写场景
        self._maybe_boot(subject_id, current, activity.id)
        # 阶段⑤ 认知活动（一次调用；不执行同轮补召回）
        # 流式中途不早开口：等整份 response_plan 齐了，再在写场前把语言+动作合成一条交付，
        # 避免口头先出、动作拖到写场后另起一行。
        verbal_delivered = False

        def deliver_verbal(value: str) -> None:
            nonlocal verbal_delivered
            if on_verbal is not None and not verbal_delivered and value.strip():
                verbal_delivered = True
                on_verbal(value)

        response, working_recalled = self._cognize(
            subject_id, activity, current, perception, clock=clock,
            on_reply=deliver_verbal if on_verbal is not None else None,
        )
        tool_ids = dict(current.tool_codes)
        response = replace(response,
            tool_consumed=tuple(tool_ids.get(token, token) for token in response.tool_consumed),
            tool_handling=tuple({**item, "task_id": tool_ids.get(item.get("task_id"), item.get("task_id"))}
                                for item in response.tool_handling))
        response = replace(response, speaker_judgments=(), next_jev_note='', object_assessment=None)
        if on_conversation_review is not None and envelope is not None:
            response, envelope, reviewed_speaker = on_conversation_review(response, envelope)
            if reviewed_speaker is not None:
                speaker = reviewed_speaker
                objects = _speaker_object_mapping(speaker, objects, speaker.aliases)
            current = replace(current, input_text=envelope.text, input_review_text='',
                              speaker=self._assembly_speaker(speaker=speaker, object_id=None))
        deliver_verbal(verbal_text(response.response_plan))
        if on_voice_plan is not None:
            on_voice_plan(response.response_plan)
        self.last_model_response = response
        if on_reply is not None:
            early = format_speech_with_action(
                verbal_text(response.response_plan),
                response.response_plan.embodied_text(),
            )
            on_reply(early)
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

        clock.mark("06标记")
        # 阶段⑥：10 按 item 分发。verbal 落记录并走独立语音占位；embodied 走肢体占位。
        # think/ignore/wait 不说话、不发音，活动仍结束。结果反馈与语言同级。
        plan = response.response_plan
        spoken_text = verbal_text(plan)
        spoke = bool(spoken_text)
        embodied = should_trigger_embodied(plan)
        action_id = ""
        if (spoke or embodied) and (action_allowed is None or action_allowed()):
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
            reply_text = spoken_text
            if current.audio_delivery:
                reply_text = f"（准备通过语音说，实际交付待播放器反馈）{reply_text}"
            embodied_text = plan.embodied_text().strip()
            if embodied_text:
                reply_text = f"{reply_text}（动作：{embodied_text}）"
            self.activity_ledger.append_subject_reply(
                subject_id,
                text_raw=code_restore(reply_text) if code_restore else reply_text,
                objects=objects,
                mentioned_object_ids=mentioned_object_ids,
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
                objects=objects,
                mentioned_object_ids=mentioned_object_ids,
                source_ids=(activity.id,),
                response_plan=plan_payload,
                response_statuses=final_statuses,
            )
        clock.mark("⑥行动")
        pack_id = self.style_packs.get(subject_id)
        registry = getattr(self.style_packs, "registry", None)
        # Always save the input/output before returning. Failed writing retains it.
        label = speaker.label if speaker else "未知来源"
        plain = _plain_turn_input(current.input_text)
        event_blocks = ["（空闲观察，距上次交往已过3分钟，没有新发言）"] if idle else [f'{label}说：“{plain}”']
        if spoken_text:
            prefix = "我准备说（尚未确认播放）" if current.audio_delivery else "我说"
            for item in plan.items:
                if item.channel == "verbal" and item.text.strip():
                    block = f'{prefix}：“{item.text}”'
                    if item.target_ids:
                        block += "（回应对象：" + "、".join(self._object_display(oid) for oid in item.target_ids) + "）"
                    event_blocks.append(block)
        if plan.embodied_text().strip() and plan.embodied_text().strip() != "无动作":
            action = "（动作计划：" + plan.embodied_text() + "）"
            if spoken_text:
                event_blocks[-1] += action
            else:
                event_blocks.append(action)
        if plan.unsaid_text().strip():
            event_blocks.append(format_unsaid_zone_block(plan.unsaid_text()))
        if code_restore:
            event_blocks = [code_restore(block) for block in event_blocks]
        event_text = f"[{activity.created_at.isoformat()}] " + "\n".join(event_blocks)
        with self._write_lock:
            self.pending_scene.append(subject_id, activity.id, event_text, blocks=event_blocks,
                sequence=max((segment.sequence for segment in self.activity_ledger.list_experiences(subject_id)), default=0),
                input_ids=tuple(u.input_id for u in envelope.current_utterances) if envelope else (fact.id,),
                prompt_level=self._prompt_profile(current).level)
            self._write_payloads[activity.id] = (
                subject_id, activity, current, response, speaker, working_recalled,
                code_restore, pack_id, registry,
            )
        self._latest_write_payloads[subject_id] = self._write_payloads[activity.id]
        self._deferred_write = lambda: self._flush_pending_scene(subject_id)
        if not defer_write:
            self._deferred_write()
            self._deferred_write = None
            clock.mark("16编排")
        # 落库 05 的工具指示：CLI 里 `jshi tool-log --turn` 靠它回看主流程这一拍交给 200 的是什么。
        if action_allowed is None or action_allowed():
            self._record_tool_intent(activity, response)
        # 只有 05 标明已消化的条目才算送达并回写；其余下一轮仍作为未消化材料出现。
        consumed = tuple(
            item for item in response.tool_consumed if item in set(turn_tool_ids)
        )
        handling = response.tool_handling
        if not handling and consumed:
            # Legacy responses retain their old contract, but unsaid must stay
            # attached to an open work item rather than vanish from context.
            handling = tuple({"task_id": task_id,
                              "disposition": "deferred" if response.response_plan.unsaid_text() else "answered",
                              "evidence": response.response_plan.verbal_text(),
                              "work_complete": True} for task_id in consumed)
        if current.audio_delivery:
            # Generated text is not yet delivered speech. The voice host can
            # confirm answered items once the player finishes the full reply.
            handling = tuple({**item, "disposition": "deferred", "work_complete": False,
                              "reason": "awaiting_voice_delivery"}
                             if item.get("disposition") == "answered" else item for item in handling)
        if handling:
            consumed = self.tool_service.handle_results(
                subject_id, speaker.object_id if speaker else "", handling,
                selected_ids=turn_tool_ids, reply=response.response_plan.verbal_text(),
                unsaid=response.response_plan.unsaid_text(),
                new_need=response.tool_intent.need if response.tool_intent else "",
            )
        handled_ids = tuple(dict.fromkeys(
            (*consumed, *(str(item.get("task_id")) for item in handling
                          if item.get("task_id") in set(turn_tool_ids)))
        ))
        self._tool_write_back(handled_ids, response.response_plan)
        self._tool_mark_main_seen(
            subject_id,
            speaker.object_id if speaker is not None else "",
            handled_ids,
        )
        self._intake_tool_if_requested(subject_id, activity, speaker, response, current.input_text)
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
        clock.mark("⑦收尾")
        try:
            self.effectiveness.run_due(subject_id)
        except Exception:
            logger.exception("effectiveness run_due failed")
        if close_result.handoff_to_memory_control:
            memory_result = self.memory_control.run_async(subject_id)
            self.last_memory_control = memory_result
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
        clock.mark("30投递")
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
        timing = clock.finish(completed.id)
        self.last_activity_timing = timing
        try:
            append_timing(
                self._timings_path,
                subject_id=subject_id,
                activity_id=timing.activity_id,
                started_at=timing.started_at,
                total_ms=timing.total_ms,
                steps=timing.steps,
            )
        except Exception:
            logger.exception("activity timing persist failed")
        # 300：事件结果钩子——只入队，不等待；执行由宿主在两拍之间 drain
        self._maybe_introspect_after_activity(
            subject_id, completed, plan, action_id, speaker
        )
        return SubjectActivityResult(
            completed,
            perception,
            plan,
            spoken_text,
            current,
            speaker=speaker,
            timing=timing,
            selected_tool_ids=turn_tool_ids,
        )

    def _governing_rules(self, subject_id: str) -> tuple[str, ...]:
        """把超级权限用户写入的提示词规则格式化成可渲染的附加规则行。"""
        if self.prompt_rules is None:
            return ()
        rules: list[str] = []
        for rule in self.prompt_rules.list(subject_id):
            if rule.section == "general":
                rules.append(rule.content)
            else:
                rules.append(f"[{rule.section}] {rule.content}")
        return tuple(rules)

    def _record_recall_trace(
        self,
        subject_id: str,
        activity_id: str,
        current: AssembledCurrentState,
        *,
        input_segment_id: str,
        query: str,
        object_id: str,
    ) -> None:
        memory_report = next(
            (item for item in current.source_report if item.source == "memory"),
            None,
        )
        recalled = tuple(
            item.split(":", 1)[-1]
            for item in (memory_report.loaded_ids if memory_report is not None else ())
        )
        skipped = tuple(
            item.split(":", 1)[-1]
            for item in (memory_report.skipped_ids if memory_report is not None else ())
        )
        try:
            self.recall_traces.append(
                RecallTrace(
                    subject_id=subject_id,
                    activity_id=activity_id,
                    query=query,
                    object_id=object_id,
                    input_segment_id=input_segment_id,
                    level=self.recall_strategy.get(subject_id).default_level,
                    recalled_event_ids=recalled,
                    skipped_ids=skipped,
                )
            )
        except Exception:
            logger.exception("recall_trace persist failed")

    def _style_fields(
        self, subject_id: str, view
    ) -> tuple[str, bool]:
        """旧「写法槽」已废弃：人格自带整份提示词，注入槽恒空。first 仅供审计。"""
        pack_id = self.style_packs.get(subject_id)
        text = ""
        zone_pack = ""
        if view is not None:
            text = getattr(view, "context_text", "") or ""
            zone_pack = getattr(view, "style_pack_id", "") or ""
        first = is_first_style_turn(text, zone_pack, pack_id)
        return "", first

    def _persona_fields(
        self,
        subject_id: str,
        current: AssembledCurrentState | None = None,
    ) -> tuple[str, Mapping | None, bool, int]:
        """非木头人格：返回 (instruction, schema, boot, zone_chars)。

        选了人格就用人格的整份提示词（不因素材不足退回木头）。
        boot 标志 = 当前无片场；真正发 boot 请求只在 ``_maybe_boot``，
        且仅当片场仍为空。
        """
        pack_id = self.style_packs.get(subject_id)
        registry = getattr(self.style_packs, "registry", None)
        if not is_persona(pack_id, registry=registry):
            return "", None, False, 0
        zone_chars = zone_chars_for(pack_id, registry=registry)
        instruction = reply_instruction_for(
            pack_id, registry=registry, zone_chars=zone_chars,
            prompt_level=self._prompt_profile(current).level if current else 1,
            prompt_modules=self._prompt_profile(current).modules if current else (),
        )
        return (
            instruction,
            reply_schema_for(pack_id, registry=registry, prompt_level=self._prompt_profile(current).level if current else 1),
            self.zone_store.empty(subject_id),
            zone_chars,
        )

    @staticmethod
    def _prompt_profile(current):
        from jshi.core.prompt_profile import PromptProfile, valid_level
        level = valid_level(current.prompt_level)
        return PromptProfile(level, recall_memory=current.recall_memory)

    def _material_chars(self, current: AssembledCurrentState) -> int:
        """木头攒下的素材字数：账本原文(活跃区) + 回忆 + 价值。"""
        total = 0
        subject_id = current.subject_state.subject_id
        for segment in self.activity_ledger.list_experiences(subject_id):
            total += len(segment.text_raw or "")
        for fragment in current.fragments:
            if getattr(fragment, "source", None) == "memory":
                total += len(str(getattr(fragment, "content", "") or ""))
        for value in current.subject_state.salient_values:
            total += len(str(value or ""))
        return total

    def _persona_ready(self, subject_id: str, current: AssembledCurrentState) -> bool:
        """人格本轮该不该生效：已有片场，或素材量已达预算一半。"""
        if not self.zone_store.empty(subject_id):
            return True
        pack_id = self.style_packs.get(subject_id)
        registry = getattr(self.style_packs, "registry", None)
        zone_chars = zone_chars_for(pack_id, registry=registry)
        return self._material_chars(current) >= zone_chars // 2

    def _memory_lines(
        self,
        fragments: Sequence[AssemblyFragment],
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """把组装里的 memory 片段转成「[时间]（名字）正文」行。"""
        from jshi.models.prompt import format_memory_line

        lines: list[str] = []
        for fragment in fragments:
            if getattr(fragment, "source", None) != "memory":
                continue
            content = str(getattr(fragment, "content", "") or "").strip()
            if not content:
                continue
            label = self._object_display(getattr(fragment, "object_id", None))
            historical_ids = getattr(fragment, "interlocutor_object_ids", ()) or ()
            if not historical_ids and getattr(fragment, "interlocutor", None):
                historical_ids = (fragment.interlocutor,)
            historical_interlocutors = tuple(
                self._object_display(object_id) for object_id in historical_ids if object_id
            )
            line = format_memory_line(
                content,
                label=label,
                object_role=getattr(fragment, "query_object_role", None),
                historical_interlocutors=historical_interlocutors,
                occurred_at=getattr(fragment, "occurred_at", None),
                now=now,
            )
            if line:
                lines.append(line)
        return lines

    def _person_experience_lines(
        self, fragments: Sequence[AssemblyFragment], codes: Mapping[str, str] | None = None
    ) -> list[str]:
        lines = []
        for fragment in fragments:
            if fragment.source != "person_experience" or not fragment.content.strip():
                continue
            label = self._object_display(fragment.object_id)
            code = (codes or {}).get(fragment.object_id)
            who = f"{code}（{label}）" if code else label
            lines.append(f"{who}：{fragment.content}")
        return lines

    def _person_portrait_lines(
        self, fragments: Sequence[AssemblyFragment], *, budget_chars: int | None = None
    ) -> list[str]:
        lines = []
        for fragment in fragments:
            if fragment.source != "person_portrait" or not fragment.content.strip():
                continue
            content = fragment.content
            choices = [s for s in fragment.alternatives if s.strip()]
            if budget_chars is not None and choices:
                fitting = [s for s in choices if len(s) <= budget_chars]
                content = max(fitting, key=len) if fitting else min(choices, key=len)
            selected = dict(fragment.portrait_levels).get({800:'L1',400:'L2',200:'L3'}.get(budget_chars,''))
            if isinstance(selected,str) and selected.strip() and len(selected) <= budget_chars:
                content = selected
            if budget_chars is not None:
                from jshi.memory.portrait_views import bounded_portrait
                content = bounded_portrait(content, budget_chars)
            lines.append(f"{self._object_display(fragment.object_id)}：{content}")
        return lines

    def _persona_user_text(
        self,
        current: AssembledCurrentState,
        *,
        boot: bool,
        include_tool: bool = True,
        now: datetime | None = None,
        live_zone: bool = False,
    ) -> str:
        label = current.speaker.label if current.speaker else "对方"
        stamp = now or datetime.now().astimezone()
        memories = self._memory_lines(current.fragments, now=stamp)
        experiences = self._person_experience_lines(current.fragments, dict(getattr(current, 'object_codes', ())))
        from jshi.style.packs import SMITH
        profile = self._prompt_profile(current)
        portrait_budget = (800 if live_zone else {1: 800, 2: 400, 3: 200}[profile.level]) if self.style_packs.get(current.subject_state.subject_id) == SMITH else None
        portraits = [*self._person_portrait_lines(current.fragments, budget_chars=portrait_budget), *self._address_notes()]
        if boot:
            return self._boot_user_text(current, label, memories, experiences=experiences,
                                        portraits=portraits, now=stamp)
        subject_id = current.subject_state.subject_id
        graded = self.style_packs.get(subject_id) == SMITH and not live_zone
        if graded:
            if not profile.includes("memory"):
                memories = []
        scene = self._zone_text_for_turn(subject_id, stamp, live=live_zone,
            max_tier=profile.scene_view if graded else 3,
            show_tiers=self.style_packs.get(subject_id) == SMITH and live_zone)
        pack_id = self.style_packs.get(subject_id)
        registry = getattr(self.style_packs, "registry", None)
        cap = zone_chars_for(pack_id, registry=registry)
        tool_chars = self._tool_block_chars(subject_id)
        parts = [
            f"【此时的片场】\n{scene or '（片场为空）'}",
        ]
        if current.interaction_scene and not live_zone:
            parts.append('【JEV情境参考】\n' + current.interaction_scene +
                         '\n这是此前完成的情境及复核建议，可能滞后；本批原话优先。由你判断受话对象与是否回应，不重新裁定人物归属。')
        if not graded:
            parts.append(format_zone_budget_note(
                self._zone_body_chars_now(subject_id),
                cap,
                tool_chars=tool_chars,
                tool_cap=self._tool_block_cap(),
            ))
        turn = (current.input_text or "").strip()
        if not current.audio_delivery:
            turn = format_turn_input(label, current.input_text, stimulus=current.stimulus)
        if current.input_items and not live_zone:
            from jshi.core.main_input import render_inputs
            normalized = render_inputs(current.input_items)
            turn = normalized
        parts.append("【此时的输入】\n" + turn)
        if memories:
            parts.append("【你此时的回忆】\n" + "\n".join(memories))
        if profile.recall_memory and any(r.source == 'memory' and r.error for r in current.source_report):
            parts.append('【回忆可用性】\n本轮历史回忆未能及时取得；不能据此断言不存在过去经历，不编造缺失的往事。')
        if portraits:
            parts.append(
                "【人物肖像·可修订】\n这是过往白描形成的描述，需以本轮信息为准。\n"
                + "\n".join(portraits)
            )
        if experiences:
            parts.append(
                "【人物相处经验·可修订】\n"
                "这是基于过往事件形成的相处认识，不能当作此人的绝对事实或某次具体往事。"
                "以本轮明确请求为先，只在相关情境使用，不要自动套用旧偏好。\n"
                + "\n".join(experiences)
            )
        extra = (current.tool_input or "").strip() if include_tool and profile.includes("tool") else ""
        if extra:
            parts.append(extra)
        hot = (current.tool_hot_state or "").strip() if include_tool and profile.includes("tool") else ""
        if hot:
            parts.append(hot)
        return "\n\n".join(parts)

    def _boot_user_text(
        self,
        current: AssembledCurrentState,
        label: str,
        memories: Sequence[str],
        *,
        experiences: Sequence[str] = (),
        portraits: Sequence[str] = (),
        now: datetime | None = None,
    ) -> str:
        subject_id = current.subject_state.subject_id
        parts: list[str] = []
        active: list[str] = []
        # 只取最近一段素材窗口，避免拿全量历史拖慢 boot / 触发超时。
        recent: list[tuple[datetime | None, str]] = []
        total = 0
        for segment in reversed(self.activity_ledger.list_experiences(subject_id)):
            text = (segment.text_raw or "").strip()
            if not text:
                continue
            total += len(text)
            recent.append((segment.occurred_at, text))
            if total >= 1200 or len(recent) >= 12:
                break
        from jshi.models.prompt import relative_time_label

        stamp_now = now or datetime.now().astimezone()
        for when, text in reversed(recent):
            stamp = relative_time_label(when, now=stamp_now)
            active.append(f"[{stamp}] {text}" if stamp else text)
        if active:
            parts.append("【素材·活跃区】\n" + "\n".join(active))
        if memories:
            parts.append("【素材·回忆】\n" + "\n".join(memories))
        if portraits:
            parts.append("【素材·人物肖像（可修订）】\n" + "\n".join(portraits))
        if experiences:
            parts.append("【素材·人物相处经验（可修订）】\n" + "\n".join(experiences))
        values = [
            str(item).strip()
            for item in current.subject_state.salient_values
            if str(item).strip()
        ]
        if values:
            parts.append("【素材·价值】\n" + "\n".join(values))
        if not parts:
            parts.append(f"【素材·活跃区】\n{label}：{current.input_text}")
        return "\n\n".join(parts)

    def _maybe_boot(
        self, subject_id: str, current: AssembledCurrentState, activity_id: str
    ) -> None:
        """非木头人格且无片场：跑一次性 boot（写场景）并存入块级片场。

        已有片场不得再 boot：``_persona_ready`` 在已有现场时为真（本轮走
        续写），不能当作 boot 条件，否则会整份覆盖现场。
        """
        pack_id = self.style_packs.get(subject_id)
        registry = getattr(self.style_packs, "registry", None)
        if not is_persona(pack_id, registry=registry):
            return
        if not self.zone_store.empty(subject_id):
            return
        if not self._persona_ready(subject_id, current):
            return
        boot_instruction = boot_instruction_for(pack_id, registry=registry)
        boot_instruction = boot_instruction.replace(
            "{zone_chars}", str(zone_chars_for(pack_id, registry=registry))
        )
        boot_schema = boot_schema_for(pack_id, registry=registry)
        if not boot_instruction or not boot_schema:
            return
        speaker = current.speaker
        model_speaker = None
        if speaker is not None:
            model_speaker = ModelSpeaker(
                object_id=speaker.object_id,
                label=speaker.label,
                aliases=speaker.aliases,
                status=speaker.status,
                reason=speaker.reason,
            )
        request = ModelRequest(
            purpose="subject_activity",
            input_text=current.input_text,
            subject_state=current.subject_state,
            speaker=model_speaker,
            persona_instruction=boot_instruction,
            persona_schema=boot_schema,
            boot=True,
            persona_user_text=self._persona_user_text(current, boot=True),
        )
        try:
            self._record_step_input(
                request, self.cognition, subject_id=subject_id, activity_id=activity_id
            )
            response = self.cognition.generate(request)
            scene = getattr(response, "scene", ())
            self.last_boot = "是"
        except Exception:
            logger.exception("boot scene failed")
            self.last_boot = "失败"
            return
        if not self.zone_store.empty(subject_id):
            return
        # 价值叙述是人格给定的固定文本，不经模型生成。
        value = (registry or self.style_packs.registry).resolve(pack_id).value_narration
        if scene or value:
            value_cap = value_narration_chars_for(pack_id, registry=registry)
            self.zone_store.boot(subject_id, value=value, scene=scene, value_cap=value_cap)

    def _zone_append_texts(
        self,
        current: AssembledCurrentState,
        reply_response,
        code_restore: Callable[[str], str] | None = None,
    ) -> tuple[str, ...]:
        """One append order for both writer A references and actual persistence."""
        plan = reply_response.response_plan
        reply_text = plan.verbal_text().strip()
        action_text = plan.embodied_text().strip()
        unsaid_block = format_unsaid_zone_block(plan.unsaid_text())
        parts: list[str] = []
        if reply_text:
            prefix = "我准备说" if current.audio_delivery else "我说"
            parts.append(f"{prefix}：“{reply_text}”")
        if action_text and action_text != "无动作":
            parts.append(action_text)
        reply_block = " ".join(parts).strip()
        restore = code_restore
        if restore is not None:
            reply_block = restore(reply_block)
            unsaid_block = restore(unsaid_block)
        label = current.speaker.label if current.speaker else "对方"
        inbound = (current.input_text or "").strip()
        restore = code_restore
        if restore is not None and inbound:
            inbound = restore(inbound)
        input_block = f"{label}说：“{inbound}”" if inbound else ""
        if current.pending_write_text:
            input_block = current.pending_write_text.split("\n", 1)[-1]
            reply_block = unsaid_block = ""
        return current.pending_write_blocks or tuple(value for value in (input_block, reply_block, unsaid_block) if value)

    def _apply_zone_edit(self, subject_id, reply_response, current, write_result, code_restore=None):
        append_texts = self._zone_append_texts(current, reply_response, code_restore)
        if append_texts or getattr(write_result, "zone_edit", ()):
            edits = tuple(getattr(write_result, "zone_edit", ()) or ())
            restore = code_restore
            if restore is not None and edits:
                restored = []
                for op in edits:
                    item = dict(op)
                    if item.get("text"):
                        item["text"] = restore(str(item["text"]))
                    restored.append(item)
                edits = tuple(restored)
            self.zone_store.apply_edit(
                subject_id,
                edits,
                covered_event_ids=current.pending_write_ids,
                append_blocks_with_time=tuple(
                    ZoneBlock(text=item, origin=(
                        "speech_plan" if item.startswith("我准备说") else
                        "delivery_fact" if item.startswith("（播放器") else ""))
                    for item in append_texts
                ),
            )

    def _tool_reap_stale(self, subject_id: str, object_id: str | None) -> None:
        service = getattr(self, "tool_service", None)
        if service is None or not object_id:
            return
        try:
            service.reap_stale(subject_id, object_id)
        except Exception:
            logger.exception("tool reap failed")

    def _tool_mark_main_seen(
        self, subject_id: str, object_id: str, item_ids: tuple[str, ...] = ()
    ) -> None:
        """本轮认知已看到【工具相关】：标记可见句已送入主流程，不写片场。"""
        if not object_id:
            return
        service = getattr(self, "tool_service", None)
        if service is None:
            return
        service.mark_main_seen(subject_id, object_id, item_ids=item_ids)

    def _tool_scene_blocks(
        self, subject_id: str, object_id: str
    ) -> tuple[tuple[ZoneBlock, ...], tuple[str, ...]]:
        """兼容旧调用：不再把工具结果写成片场「（我知道）」块。"""
        if not object_id:
            return (), ()
        service = getattr(self, "tool_service", None)
        if service is None:
            return (), ()
        delivered = service.mark_main_seen(subject_id, object_id)
        return (), delivered

    def _revoke_tool_delivery(self, task_ids: tuple[str, ...]) -> None:
        """撤销「已送入主流程」标记。"""
        if not task_ids:
            return
        service = getattr(self, "tool_service", None)
        if service is None:
            return
        for task_id in task_ids:
            service.revoke_delivery(task_id)

    def _zone_body_chars_now(self, subject_id: str) -> int:
        """片场正文口径的字数：**不含**块号后显示用的时间方括号。

        预算按正文算，否则块一多，时间标签会把预算悄悄吃掉。
        """
        from jshi.models.prompt import relative_time_label

        label_chars = sum(
            len(label) + 2
            for label in (
                relative_time_label(block.at)
                for block in self.zone_store.blocks(subject_id)
            )
            if label
        )
        return max(0, self.zone_store.body_chars(subject_id) - label_chars)

    def _tool_block_chars(self, subject_id: str) -> int:
        """片场里工具块的已用字数（单独一档预算；按正文算，不含时间标签）。"""
        from jshi.tool.service import SCENE_BLOCK_PREFIX

        return sum(
            len(block.text)
            for block in self.zone_store.blocks(subject_id)
            if block.text.startswith(SCENE_BLOCK_PREFIX)
        )

    @staticmethod
    def _tool_block_cap() -> int:
        """工具块这一档的总预算：单条上限 × 同场条数上限。"""
        from jshi.tool.service import SCENE_BLOCK_CAP, SCENE_BLOCK_CHARS

        return SCENE_BLOCK_CHARS * SCENE_BLOCK_CAP

    def _tool_write_back(self, task_ids: tuple[str, ...], plan) -> None:
        """回写：本轮回应进入这几本记挂（回应可以完全不提工具）。"""
        if not task_ids:
            return
        service = getattr(self, "tool_service", None)
        if service is None:
            return
        reply = plan.verbal_text().strip()
        action = plan.embodied_text().strip()
        response = {
            "mode": str(getattr(plan, "mode", "") or ""),
            "reply": reply,
            "action": action if action != "无动作" else "",
            "text": " ".join(item for item in (reply, action) if item and item != "无动作"),
        }
        try:
            service.write_back_response(task_ids, response)
        except Exception:
            logger.exception("tool response write-back failed")

    def _record_step_input(
        self,
        request: ModelRequest,
        model: object,
        *,
        subject_id: str,
        activity_id: str,
    ) -> None:
        """存渲染后的系统提示词和用户提示词。失败不挡说话。"""
        try:
            rendered = request
            skill = getattr(model, "_skill", None)
            apply_to = getattr(model, "_apply_to", ())
            if skill is not None and request.purpose in apply_to:
                rendered = replace(request, system_extra=skill.system_extra(request))
            from jshi.models.prompt import build_system, build_user

            self.step_inputs.append_call(
                subject_id=subject_id,
                activity_id=activity_id,
                purpose=request.purpose,
                model=str(getattr(model, "name", "") or ""),
                system_text=build_system(rendered),
                user_text=build_user(rendered),
            )
        except Exception:
            logger.exception("step input persist failed")

    def _cognize_once(
        self,
        current: AssembledCurrentState,
        working_recalled: Sequence[RecalledFragment],
        *,
        activity_id: str,
        on_reply: Callable[[str], None] | None = None,
    ) -> object:
        """05 认知层：一次模型调用，只产出回应与上下文方案，不执行记忆。

        当底层模型支持流式且调用方给了 ``on_reply`` 时，走 ``generate_stream``:
        ``response_plan`` 一旦完整且 mode=respond 就先回调 ``on_reply``(提前开口),
        其余段(活跃区/打分)继续收、最终返回完整 ``ModelResponse``。
        """
        speaker = None
        if current.speaker is not None:
            speaker = ModelSpeaker(
                object_id=current.speaker.object_id,
                label=current.speaker.label,
                aliases=current.speaker.aliases,
                status=current.speaker.status,
                reason=current.speaker.reason,
            )
        style_instruction, style_first = self._style_fields(
            current.subject_state.subject_id, current.context_view
        )
        persona_instruction, persona_schema, _boot, _zone_chars = self._persona_fields(
            current.subject_state.subject_id, current
        )
        now = datetime.now().astimezone()
        with self._write_lock:
            self._reconcile_pending_scene(current.subject_state.subject_id)
            current = replace(current, context_view=self.active_zone.load(current.subject_state.subject_id))
            persona_user_text = (
                self._persona_user_text(current, boot=False, now=now) if persona_instruction else ""
            )
            pending_text = self.pending_scene.render(current.subject_state.subject_id)
        extra_context = ({"kind": "pending_scene", "content": pending_text},) if pending_text and not persona_instruction else ()
        if current.interaction_scene and not persona_instruction:
            extra_context += ({'kind': 'jev_scene', 'content': current.interaction_scene},)
        request = ModelRequest(
            purpose="subject_activity",
            input_text=current.input_text,
            subject_state=current.subject_state,
            speaker=speaker,
            now=now,
            governing_rules=self._governing_rules(
                current.subject_state.subject_id
            ),
            style_instruction=style_instruction,
            style_first=style_first,
            persona_instruction=persona_instruction,
            persona_schema=persona_schema,
            persona_user_text=persona_user_text,
            tool_input=current.tool_input,
            transport_context=current.transport_context,
            tool_hot_state=current.tool_hot_state,
            input_review_text=current.input_review_text,
            processing_modes=current.processing_modes,
            input_items=current.input_items,
            input_parts=current.input_parts,
            stimulus=current.stimulus,
            context=self._model_context(
                current.subject_state.subject_id,
                working_recalled,
                current.fragments,
                current.context_view,
            ) + extra_context,
        )
        self._record_step_input(
            request,
            self.cognition,
            subject_id=current.subject_state.subject_id,
            activity_id=activity_id,
        )
        if persona_instruction and self.style_packs.get(current.subject_state.subject_id) == "smith":
            profile = self._prompt_profile(current)
            self.repository.add_history(HistoryRecord(
                subject_id=current.subject_state.subject_id, kind=HistoryKind.SUBJECT,
                event_type="main_prompt_profile", content={
                    "activity_id": activity_id, "level": profile.level, "prompt_policy_version": 3,
                    "suggested_level": current.prompt_level,
                    "upgrade_reason": "",
                    "recall_memory": profile.recall_memory,
                    "modules": [n for n in ("tool", "memory", "person") if profile.includes(n)],
                    "scene_view": profile.scene_view,
                    "instruction_chars": len(persona_instruction), "user_chars": len(persona_user_text),
                }))
        if on_reply is not None and hasattr(self.cognition, "generate_stream"):
            response = self.cognition.generate_stream(request, on_reply=on_reply)
        else:
            response = self.cognition.generate(request)
        if persona_instruction and self.style_packs.get(current.subject_state.subject_id) == "smith" and self._prompt_profile(current).level != 1:
            if response.tool_intent or response.tool_handling or response.tool_consumed:
                self.repository.add_history(HistoryRecord(subject_id=current.subject_state.subject_id,
                    kind=HistoryKind.SUBJECT, event_type="unexpected_tool_output",
                    content={"activity_id": activity_id, "level": self._prompt_profile(current).level}))
                intent = None
                # The first verbal plan may already have streamed. Never replace
                # it with a second, unplayed answer or claim its results delivered.
                response = replace(response, tool_intent=intent, tool_handling=(), tool_consumed=())
        return response

    def _wood_write_user_text(
        self, current: AssembledCurrentState, reply_text: str, unsaid_text: str = ""
    ) -> str:
        """木头写场调用的 user：与写场提示词逐项对应（含上一份现场 / 本轮原话 / 你的回应 / 未说出口 / 新回忆 / 说话人）。

        ``build_user`` 遇非空 ``persona_user_text`` 会原样返回，故木头写场用它承载
        「你的回应」这一项（否则 ``build_user`` 只渲染 说话人/活跃区/回忆/本轮，丢掉落回应）。
        """
        label = current.speaker.label if current.speaker else "对方"
        parts: list[str] = [f"【说话人】{label}"]
        prev = (
            current.context_view.context_text.strip()
            if current.context_view is not None and current.context_view.context_text
            else ""
        )
        if prev:
            parts.append(f"【上一份现场】\n{prev}")
        parts.append(f"【本轮原话】{label}：{current.input_text}")
        if current.input_review_text:
            parts.append("【归属说明】原话的姓名与标记来自入口，主流程人物判断只是建议。“上下文推测”等标记须保留；候选与JEV短评是工作材料，不作为人物原话或已确认身份写入现场。")
        if reply_text:
            prefix = "我准备说" if current.audio_delivery else "我说"
            parts.append(f"【你的回应】{prefix}：{reply_text}")
        unsaid_block = format_unsaid_zone_block(unsaid_text)
        if unsaid_block:
            parts.append(f"【未说出口】{unsaid_block}")
        memories = self._memory_lines(current.fragments)
        if memories:
            parts.append("【本轮新回忆】\n" + "\n".join(memories))
        return "\n".join(parts)

    @property
    def unknown_inputs(self):
        with self._write_lock:
            if self._unknown_inputs is None:
                from jshi.core.unknown_inputs import UnknownInputs
                self._unknown_inputs = UnknownInputs(self.repository.path.parent / "unknown_inputs.sqlite3")
            return self._unknown_inputs

    def schedule_scene_write(self, subject_id: str) -> None:
        """Interactive text host: coalesced wakeups on an independent writer."""
        with self._write_lock:
            self._scene_subjects.add(subject_id)
            self._scene_wakeup.set()
            if self._scene_thread is None or not self._scene_thread.is_alive():
                self._scene_thread = threading.Thread(target=self._scene_writer, daemon=True,
                                                      name="jshi-scene-writer")
                self._scene_thread.start()

    def _scene_writer(self) -> None:
        while True:
            self._scene_wakeup.wait()
            with self._write_lock:
                subjects = tuple(self._scene_subjects)
                self._scene_subjects.clear()
                self._scene_wakeup.clear()
            for subject_id in subjects:
                self._flush_pending_scene(subject_id)
            with self._write_lock:
                if not self._scene_subjects:
                    self._scene_thread = None
                    return

    def take_deferred_write(self):
        job = self._deferred_write
        self._deferred_write = None
        return job

    def _zone_text_for_turn(self, subject_id: str, now: datetime, *, live: bool, max_tier: int = 3, show_tiers: bool = False) -> str:
        """认知读取已提交片场与未整理输入输出；写场只读已提交版本。"""
        with self._write_lock:
            scene = self.zone_store.render(subject_id, now=now, max_tier=max_tier, show_tiers=show_tiers)
            self._reconcile_pending_scene(subject_id)
            pending = "" if live else self.pending_scene.render(subject_id)
            return "\n\n".join(part for part in (scene, pending) if part)

    def _reconcile_pending_scene(self, subject_id: str) -> None:
        ids = self.zone_store.covered_event_ids.get(subject_id, ())
        ids = (*ids, *getattr(self.activity_ledger.current_context_view(subject_id), "covered_event_ids", ()))
        if ids:
            self.pending_scene.complete(ids)
            for event_id in ids:
                self._write_payloads.pop(event_id, None)

    def _flush_pending_scene(self, subject_id: str) -> bool:
        # Only one scene writer, but never hold the reader lock during a model call.
        with self._scene_writer_lock:
            with self._write_lock:
                self._reconcile_pending_scene(subject_id)
                rows = self.pending_scene.snapshot(subject_id)
                if not rows:
                    return True
                payload = next((self._write_payloads.get(row["id"]) for row in reversed(rows)
                                if row["id"] in self._write_payloads), self._latest_write_payloads.get(subject_id))
                if payload is None:
                    return False  # Restored records stay visible until a new turn supplies a writer.
                sid, activity, current, response, speaker, recalled, restore, pack, registry = payload
                current = replace(current, context_view=self.active_zone.load(subject_id), pending_write_text=self.pending_scene.render_rows(rows),
                                  prompt_level=min(row.get("prompt_level", 1) for row in rows),
                                  pending_write_ids=tuple(row["id"] for row in rows),
                                  pending_write_blocks=tuple(block for row in rows for block in row.get("blocks", (row["text"],))),
                                  pending_through_sequence=max((row.get("sequence", 0) for row in rows), default=0))
            try:
                wrote = self._commit_zone(sid, activity, current, response, speaker, recalled,
                                          restore, pack, registry)
            except Exception:
                logger.exception("pending scene batch failed; keeping input/output")
                return False
            if wrote:
                with self._write_lock:
                    for event_id in current.pending_write_ids:
                        self._write_payloads.pop(event_id, None)
            return wrote

    def note_scene_delivery(self, subject_id: str, event_id: str, text: str) -> None:
        with self._write_lock:
            self.pending_scene.append(subject_id, event_id, text)

    def note_entry_background(self, subject_id: str, utterance) -> None:
        """Retained entry observations live in scene history, never future new input."""
        if not utterance.input_id:
            return
        with self._write_lock:
            event_id = 'entry:' + utterance.input_id
            covered = (*self.zone_store.covered_event_ids.get(subject_id, ()),
                       *getattr(self.activity_ledger.current_context_view(subject_id), 'covered_event_ids', ()))
            if event_id in covered:
                return
            text = f"[入口观察，input_id={utterance.input_id}，at={utterance.received_at_ms}] {utterance.speaker.label}：{utterance.text}（未触发回应）"
            self.pending_scene.append(subject_id, event_id, text, blocks=(text,), input_ids=(utterance.input_id,))

    def person_review_scene(self, subject_id: str) -> dict:
        with self._write_lock:
            self._reconcile_pending_scene(subject_id)
            scene = self.zone_store.render(subject_id, now=datetime.now().astimezone())
            if not scene:
                scene = self.active_zone.load(subject_id).context_text or ''
            pending = self.pending_scene.render(subject_id)
            return {'committed_scene': scene[:12000], 'pending_events': pending[:16000],
                    'scene_excerpted': len(scene) > 12000, 'pending_excerpted': len(pending) > 16000}

    def _address_notes(self) -> list[str]:
        profiles = self.profiles
        if profiles is None or not hasattr(profiles, "address_line"):
            return []
        lines = []
        for object_id in getattr(self, "_voice_party_ids", ()):
            line = profiles.address_line(object_id)
            if line:
                lines.append(line)
        return lines

    def _commit_zone(
        self, subject_id, activity, current, response, speaker, working_recalled,
        code_restore, pack_id, registry,
    ) -> bool:
        started_at = datetime.now().astimezone().isoformat()
        began = time.perf_counter()
        self._record_write_timing(subject_id, activity.id, "write", "started", started_at)
        status = "error"
        try:
            wrote = self._commit_zone_impl(
                subject_id, activity, current, response, speaker, working_recalled,
                code_restore, pack_id, registry,
            )
            status = "succeeded" if wrote else "failed"
            return wrote
        finally:
            self._record_write_timing(subject_id, activity.id, "write", status, started_at,
                finished_at=datetime.now().astimezone().isoformat(),
                total_ms=round((time.perf_counter() - began) * 1000, 3))

    def _record_write_timing(self, subject_id, activity_id, phase, status, started_at, **extra):
        """写场独立实测；挂起时仍能查到开始记录，记录失败不阻断写场。"""
        payload = dict(subject_id=subject_id, activity_id=activity_id,
            phase=phase, status=status, started_at=started_at, **extra)
        try:
            with self._write_timing_lock:
                self._write_timings_path.parent.mkdir(parents=True, exist_ok=True)
                with self._write_timings_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("write timing persist failed")

    def _commit_zone_impl(
        self, subject_id, activity, current, response, speaker, working_recalled,
        code_restore, pack_id, registry,
    ) -> bool:
        written = current
        if code_restore is not None:
            written = replace(current, input_text=code_restore(_plain_turn_input(current.input_text or "")))
        write_result = (
            self._write_zone(subject_id, activity, written, response, speaker, working_recalled)
            if self.write_zone is not None
            else response
        )
        self.last_write_response = write_result
        if (getattr(write_result, "metadata", None) or {}).get("skill_fallback") == "write_zone_retry_failed":
            return False
        with self._write_lock:
            if is_persona(pack_id, registry=registry) and self._persona_ready(subject_id, written):
                self._apply_zone_edit(subject_id, response, written, write_result, code_restore)
            else:
                text = getattr(write_result, "rewritten_context", "") or ""
                if not text.strip():
                    return False
                if code_restore is not None:
                    text = code_restore(text)
                self.activity_ledger.save_rewritten_context(
                    subject_id, text,
                    speaker_object_id=speaker.object_id if speaker is not None else None,
                    style_pack_id=pack_id,
                    covered_event_ids=current.pending_write_ids,
                    through_sequence=current.pending_through_sequence,
                )
            self.pending_scene.complete(current.pending_write_ids)
        return True

    def _write_zone(
        self,
        subject_id: str,
        activity: Activity,
        current: AssembledCurrentState,
        response,
        speaker: SpeakerCandidate | None,
        working_recalled: Sequence[RecalledFragment],
    ) -> ModelResponse:
        """写场调用（②）：木头整份 ``rewritten_context`` / 人格 `edit`。

        与回复调用独立：本轮回复已由调用①交出（``response_plan``），这里只整理现场/片场。
        失败重试一次；仍失败 → 空写场结果（木头沿用上一份 / 人格片场不动），不吞回复。
        """
        pack_id = self.style_packs.get(subject_id)
        registry = getattr(self.style_packs, "registry", None)
        persona = is_persona(pack_id, registry=registry)
        model_speaker = None
        if speaker is not None:
            model_speaker = ModelSpeaker(
                object_id=speaker.object_id,
                label=speaker.label,
                aliases=speaker.aliases,
                status=speaker.status,
                reason=speaker.reason,
            )
        reply_text = (
            response.response_plan.verbal_text().strip()
            if response.response_plan is not None
            else ""
        )
        unsaid_text = (
            response.response_plan.unsaid_text()
            if response.response_plan is not None
            else ""
        )
        now = datetime.now().astimezone()
        if persona:
            persona_instruction = write_instruction_for(
                pack_id,
                registry=registry,
                zone_chars=zone_chars_for(pack_id, registry=registry),
            )
            persona_schema = write_schema_for(pack_id, registry=registry)
            persona_user_text = self._persona_user_text(
                current, boot=False, include_tool=False, now=now, live_zone=True
            )
            plan = response.response_plan
            persona_user_text += "\n\n【你的回应】\n" + json.dumps(
                {
                    "mode": plan.mode if plan is not None else "wait",
                    "items": [
                        {"channel": item.channel, "text": item.text,
                         "target_ids": [dict(current.object_codes).get(oid, self._object_display(oid)) for oid in item.target_ids]}
                        for item in plan.items
                    ] if plan is not None else [],
                    "unsaid": unsaid_text,
                }, ensure_ascii=False,
            )
        else:
            persona_instruction = ""
            persona_schema = None
            persona_user_text = self._wood_write_user_text(
                current, reply_text, unsaid_text
            )

        if current.pending_write_text:
            persona_user_text += "\n\n【本次合批整理的输入输出】\n" + current.pending_write_text.split("\n", 1)[-1]
            persona_user_text += "\n只将以上合批时间线作为本次新增事件；其他本轮原话、回应是对照材料，已包含的或此前已提交的内容不再追加。按时间顺序一起整理，计划与实际交付须区分。"
        if persona and pack_id == "smith":
            append_texts = self._zone_append_texts(current, response)
            if append_texts:
                persona_user_text += "\n\n【程序将追加的新块】\n" + "\n".join(
                    f"A{i}：{text}" for i, text in enumerate(append_texts, 1))
                persona_user_text += "\n只标这些新块的重要性tier，不重复输出正文；它们不计入现有B编号。"
        context = self._model_context(
            subject_id, working_recalled, current.fragments, current.context_view, record_mappings=False
        )
        if reply_text:
            # 木头整份重写需要知道本轮回应（写"我说：…"），并入上下文。
            context = (*context, {"kind": "subject_reply", "text": reply_text})
        if unsaid_text:
            context = (
                *context,
                {
                    "kind": "subject_unsaid",
                    "text": format_unsaid_zone_block(unsaid_text),
                },
            )
        request = ModelRequest(
            purpose="write_zone",
            transport_context=current.transport_context,
            input_text=current.input_text,
            subject_state=current.subject_state,
            speaker=model_speaker,
            now=now,
            governing_rules=self._governing_rules(subject_id),
            style_instruction="",
            style_first=False,
            persona_instruction=persona_instruction,
            persona_schema=persona_schema,
            persona_user_text=persona_user_text,
            stimulus=current.stimulus,
            context=context,
        )
        def generate(attempt):
            started_at = datetime.now().astimezone().isoformat()
            began = time.perf_counter()
            status = "error"
            self._record_write_timing(subject_id, activity.id, "model", "started", started_at,
                attempt=attempt, model=getattr(self.write_zone, "name", "write_zone"))
            try:
                result = self.write_zone.generate(request)
                if (result.metadata or {}).get("skill_fallback"):
                    raise ValueError("写场结果无法解析")
                status = "succeeded"
                return result
            finally:
                self._record_write_timing(subject_id, activity.id, "model", status, started_at,
                    attempt=attempt, model=getattr(self.write_zone, "name", "write_zone"),
                    finished_at=datetime.now().astimezone().isoformat(),
                    total_ms=round((time.perf_counter() - began) * 1000, 3))

        try:
            self._record_step_input(
                request,
                self.write_zone,
                subject_id=subject_id,
                activity_id=activity.id,
            )
            return generate(1)
        except Exception:
            logger.exception("write_zone attempt 1 failed, retrying")
            try:
                return generate(2)
            except Exception:
                logger.exception(
                    "write_zone attempt 2 failed; keeping previous zone"
                )
                return ModelResponse(
                    model=getattr(self.write_zone, "name", "write_zone"),
                    metadata={"skill_fallback": "write_zone_retry_failed"},
                    rewritten_context="",
                    zone_edit=(),
                )

    def _record_tool_intent(self, activity: Activity, response: ModelResponse) -> None:
        """05 标了「要用工具」时，把这一拍的工具指示落一条历史（只读回看用，不进 08/09）。

        200 的交接里只有 need / verbal；「这一拍到底有没有标 use_tool」只在内存里，
        CLI 看不到，所以在这里留一份到 `activity_response_marked`。
        没标「要用」就不落，避免每轮都往历史里塞一条。
        """
        intent = getattr(response, "tool_intent", None)
        if intent is None:
            return
        plan = getattr(response, "response_plan", None)
        self.repository.add_history(
            HistoryRecord(
                subject_id=activity.subject_id,
                kind=HistoryKind.FACT,
                event_type="activity_response_marked",
                content={
                    "use_tool": True,
                    "need": getattr(intent, "need", "") or "",
                    "mode": str(getattr(plan, "mode", "") or ""),
                    "reply": (plan.verbal_text() if plan is not None else "") or "",
                },
                source_ids=(activity.id,),
            )
        )

    def _bind_tool_scene_loader(self) -> None:
        """主流程的 205 按指针现读现场；调用方已注入 loader 则不覆盖。"""
        planner = getattr(self.tool_service, "planner", None)
        if planner is None or not hasattr(planner, "scene_loader"):
            return
        if planner.scene_loader is not None:
            return
        from jshi.tool.scene import bind_scene_loader

        planner.scene_loader = bind_scene_loader(self.active_zone, self.zone_store)

    def _intake_tool_if_requested(
        self,
        subject_id: str,
        activity: Activity,
        speaker: SpeakerCandidate | None,
        response: ModelResponse,
        input_text: str = "",
    ) -> None:
        """有工具指示则交接给 200；不等策划、不等执行。"""
        intent = getattr(response, "tool_intent", None)
        if intent is None:
            return
        object_id = (speaker.object_id if speaker is not None else "") or ""
        if not object_id:
            return
        pack_id = self.style_packs.get(subject_id)
        registry = getattr(self.style_packs, "registry", None)
        persona = is_persona(pack_id, registry=registry)
        if persona:
            zone_kind = "persona"
            zone_rev = activity.id
        else:
            zone_kind = "wood"
            view = self.active_zone.load(subject_id)
            zone_rev = str(view.version) if getattr(view, "version", 0) else activity.id
        self.tool_service.intake(
            subject_id=subject_id,
            object_id=object_id,
            activity_id=activity.id,
            need=intent.need or "",
            verbal=verbal_text(response.response_plan),
            field_ref={
                "activity_id": activity.id,
                "zone_kind": zone_kind,
                "zone_rev": str(zone_rev),
            },
            origin="external_05",
            refresh_reason=intent.refresh_reason or (
                input_text if any(word in input_text for word in ("重新查", "重查", "刷新", "更新数据")) else ""
            ),
            work_id=intent.work_id,
            step_id=intent.step_id,
        )

    def _cognize(
        self,
        subject_id: str,
        activity: Activity,
        current: AssembledCurrentState,
        perception: CognitiveContent,
        clock: StepClock | None = None,
        *,
        on_reply: Callable[[str], None] | None = None,
    ) -> tuple[object, list[RecalledFragment]]:
        """主流程认知编排：05 一次产出方案。同轮不执行 recall_requests。"""
        working_recalled: list[RecalledFragment] = list(current.recalled)
        response = self._cognize_once(
            current, working_recalled, activity_id=activity.id, on_reply=on_reply
        )
        if clock is not None:
            clock.mark("⑤认知")
        if getattr(response, "recall_requests", ()):
            logger.debug(
                "ignored recall_requests count=%s",
                len(response.recall_requests),
            )
        ratings = getattr(response, "memory_ratings", None)
        if ratings is not None and ratings.has_content():
            try:
                self.effectiveness.record_ratings(subject_id, activity.id, ratings)
            except Exception:
                logger.exception("memory_ratings persist failed")

        self.evaluation.emit(
            EvaluationEvent(
                event_id=new_id(),
                subject_id=subject_id,
                activity_id=activity.id,
                event_type="response_plan_ready",
                payload={
                    "mode": response.response_plan.mode,
                    "reason": response.response_plan.reason,
                    **(
                        {"skill_fallback": response.metadata.get("skill_fallback")}
                        if response.metadata and response.metadata.get("skill_fallback")
                        else {}
                    ),
                },
                source_ids=(activity.id, perception.id),
            )
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
                reason=speaker.reason,
                mentioned_object_ids=tuple(speaker.mentioned_object_ids),
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

    def _subject_display_name(self, subject_id: str) -> str:
        try:
            profile = self.identities.get(subject_id)
        except KeyError:
            return "匠石"
        return (profile.name or "").strip() or "匠石"

    def _object_display(self, object_id: str | None) -> str:
        if not object_id:
            return "匠石"
        profile = self.profiles.get(object_id) if self.profiles is not None else None
        if profile is None:
            return object_id
        if profile.label:
            if profile.aliases:
                return f"{profile.label}（{'、'.join(profile.aliases)}）"
            return profile.label
        return object_id

    def _unshorten_context_assessment(self, assessment):
        if assessment is None:
            return None
        reverse = {**self._segment_short_map, **self._memory_short_map}
        return replace(
            assessment,
            remove=tuple(reverse.get(item, item) for item in assessment.remove),
            drop_recall=tuple(reverse.get(item, item) for item in assessment.drop_recall),
            focus=tuple(reverse.get(item, item) for item in assessment.focus),
        )

    def _model_context(
        self,
        subject_id: str,
        recalled: Sequence[RecalledFragment],
        fragments: Sequence[AssemblyFragment],
        context_view: ContextViewState | None = None,
        *, record_mappings: bool = True,
    ) -> tuple[dict[str, object], ...]:
        items: list[dict[str, object]] = []
        segment_map = {}
        memory_map = {}

        actor_by_segment: dict[str, str | None] = {}
        time_by_segment: dict[str, datetime | None] = {}
        if context_view is not None:
            for segment in self.activity_ledger.list_experiences(subject_id):
                actor_by_segment[segment.segment_id] = segment.actor_object_id
                time_by_segment[segment.segment_id] = segment.occurred_at

        m_counter = 0
        manual_counter = 0
        for fragment in fragments:
            if fragment.source == "tool":
                continue
            if fragment.source == "manual_history":
                manual_counter += 1
                items.append({"id": f"U{manual_counter}", "kind": "recall_excerpt",
                    "content": fragment.content, "source": "manual_history",
                    "label": self._object_display(fragment.object_id), "status": "active"})
                continue
            if fragment.source == "memory":
                m_counter += 1
                short_id = f"M{m_counter}"
                memory_map[short_id] = fragment.id
                items.append(
                    {
                        "id": short_id,
                        "kind": fragment.kind,
                        "content": fragment.content,
                        "status": fragment.status,
                        "source": "memory",
                        "label": self._object_display(fragment.object_id),
                        "occurred_at": _iso(fragment.occurred_at),
                    }
                )
                continue
            if fragment.source == "person_experience":
                items.append(
                    {
                        "id": fragment.id,
                        "kind": "person_experience",
                        "content": fragment.content,
                        "status": fragment.status,
                        "source": fragment.source,
                        "object_id": fragment.object_id,
                        "label": self._object_display(fragment.object_id),
                        "source_event_ids": fragment.source_ids,
                        "variant": fragment.variant,
                        "updated_at": _iso(fragment.occurred_at),
                    }
                )
                continue
            if fragment.source == "person_portrait":
                items.append(
                    {
                        "id": fragment.id,
                        "kind": "portrait",
                        "content": fragment.content,
                        "status": fragment.status,
                        "source": fragment.source,
                        "object_id": fragment.object_id,
                        "label": self._object_display(fragment.object_id),
                        "updated_at": _iso(fragment.occurred_at),
                    }
                )
                continue
            items.append(
                {
                    "id": fragment.id,
                    "kind": fragment.kind,
                    "content": fragment.content,
                    "status": fragment.status,
                    "source": fragment.source,
                }
            )

        if context_view is not None:
            segments = []
            counter = 0
            for segment_id, text in context_view.segment_texts:
                counter += 1
                short_id = f"S{counter}"
                segment_map[short_id] = segment_id
                segments.append(
                    {
                        "id": short_id,
                        "text": text,
                        "label": self._object_display(actor_by_segment.get(segment_id)),
                        "occurred_at": _iso(time_by_segment.get(segment_id)),
                    }
                )
            if not segments and (context_view.context_text or "").strip():
                segments.append(
                    {
                        "id": "zone",
                        "text": context_view.context_text,
                        "label": "",
                        "occurred_at": "",
                    }
                )
            items.append(
                {
                    "id": f"context-v{context_view.version}",
                    "kind": "active_zone_refs",
                    "content": "",
                    "status": "active",
                    "source": "activity",
                    "segment_refs": list(context_view.segment_refs),
                    "segments": segments,
                    "recall_refs": [ref for ref, _text in context_view.recall_excerpts],
                    "speaker_object_id": context_view.speaker_object_id,
                }
            )

        for item in recalled:
            m_counter += 1
            short_id = f"M{m_counter}"
            ref = f"memory:{item.event_id}"
            memory_map[short_id] = ref
            items.append(
                {
                    "id": short_id,
                    "kind": "recalled_fact",
                    "content": item.content or item.text,
                    "status": "active",
                    "source": "memory",
                    "event_type": item.event_type,
                    "label": self._object_display(item.object_id),
                    "occurred_at": _iso(item.occurred_at),
                }
            )
        if record_mappings:
            self._segment_short_map = segment_map
            self._memory_short_map = memory_map
        return tuple(items)

    def _bind_recall_object_ids(
        self,
        requests: Sequence[RecallRequest],
        *,
        speaker_object_id: str | None,
    ) -> tuple[RecallRequest, ...]:
        """只读档案补全召回对象。不新建；模型已填且不是误用说话人 id 则不动。"""
        return tuple(
            self._bind_one_recall(request, speaker_object_id) for request in requests
        )

    def _bind_one_recall(
        self,
        request: RecallRequest,
        speaker_object_id: str | None,
    ) -> RecallRequest:
        existing = self._resolve_recall_object_ids(request.object_ids)
        if existing != request.object_ids:
            request = replace(request, object_ids=existing)
        hit = self._profile_named_in_query(request.query)
        if hit is None:
            return request
        if not existing:
            return replace(request, object_ids=(hit.object_id,))
        if (
            speaker_object_id
            and existing == (speaker_object_id,)
            and hit.object_id != speaker_object_id
        ):
            return replace(request, object_ids=(hit.object_id,))
        return request

    def _resolve_recall_object_ids(self, tokens: Sequence[str]) -> tuple[str, ...]:
        """模型填名字或档案 id 都收；重名名字丢掉，改由 query 绑定。"""
        if self.profiles is None:
            return tuple(str(item).strip() for item in tokens if str(item).strip())
        resolved: list[str] = []
        seen: set[str] = set()
        for raw in tokens:
            text = str(raw or "").strip()
            if not text:
                continue
            profile = self.profiles.get(text)
            if profile is None:
                named = self.profiles.find_by_names(text)
                if len(named) == 1:
                    profile = named[0]
                elif len(named) > 1:
                    continue
            object_id = profile.object_id if profile is not None else text
            if object_id in seen:
                continue
            seen.add(object_id)
            resolved.append(object_id)
        return tuple(resolved)

    def _profile_named_in_query(self, query: str) -> ObjectProfile | None:
        text = (query or "").strip()
        if not text:
            return None
        exact = self.profiles.find_by_names(text)
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None
        hay = text.casefold()
        hits: dict[str, ObjectProfile] = {}
        for profile in self.profiles.list():
            names = (profile.label, *profile.aliases)
            for name in names:
                key = name.strip()
                if len(key) < 2:
                    continue
                if key.casefold() in hay:
                    hits[profile.object_id] = profile
                    break
        if len(hits) == 1:
            return next(iter(hits.values()))
        return None

    # ------------------------------------------------------------------
    # 反思（内部活动，委托给反思系统）
    # ------------------------------------------------------------------

    def reflect(self, subject_id: str, prompt: str) -> CognitiveContent:
        return self.reflection.reflect(subject_id, prompt)

    # -- 300 自省的宿主接口 -------------------------------------------------
    # 钩子只入队；真正跑模型的动作（drain）由宿主在两拍之间调，不进本轮关键路径。

    def introspect_idle(
        self, subject_id: str, *, object_id: str | None = None
    ) -> IntrospectionRequest | None:
        """闲时回顾入口：只入队，不跑模型（何时 drain 由宿主决定）。"""
        fn = getattr(self.reflection, "enqueue_idle", None)
        if not callable(fn):
            return None
        try:
            return fn(subject_id, object_id=object_id)
        except Exception:
            logger.exception("introspect_idle failed")
            return None

    def drain_introspection(
        self, subject_id: str | None = None, *, limit: int = 1
    ) -> tuple[IntrospectionRun, ...]:
        """执行已入队的自省（宿主在两拍之间调用；失败不影响正常活动）。"""
        fn = getattr(self.reflection, "drain", None)
        if not callable(fn):
            return ()
        try:
            return tuple(fn(subject_id, limit=limit) or ())
        except Exception:
            logger.exception("drain_introspection failed")
            return ()

    def _maybe_introspect_after_activity(
        self,
        subject_id: str,
        activity: Activity,
        plan: ResponsePlan,
        action_id: str,
        speaker: SpeakerCandidate | None,
    ) -> None:
        """事件结果钩子：只入队，不等待；自省坏了不能影响本轮。"""
        fn = getattr(self.reflection, "enqueue_from_sources", None)
        if not callable(fn):
            return
        try:
            fn(
                MOMENT_AFTER_ACTIVITY,
                subject_id,
                activity=activity,
                plan=plan,
                action_id=action_id,
                object_id=(speaker.object_id if speaker is not None else None),
            )
        except Exception:
            logger.exception("introspection trigger failed")

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
        if kind is PersonalKind.VALUE:
            raise ValueError(
                "价值观与边界须走 100（import-values / propose-value），"
                "不要用 add-personal"
            )
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
