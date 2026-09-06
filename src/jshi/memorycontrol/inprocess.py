from __future__ import annotations

import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from jshi.core.params import active_zone_chars
from jshi.experienceledger import ConsumerKind, ExperienceLedgerPort, OutputKind
from jshi.memory.contracts import (
    MemoryBatch,
    MemoryExperience,
    new_id,
)

from .port import (
    MemoryBatchIngestPort,
    MemoryControlPort,
    MemoryControlResult,
    MemoryProcessStatus,
    MemoryTriggerDecision,
)


def local_now() -> datetime:
    """夜间窗口等墙钟概念用本地时间；时间戳仍走 UTC。"""
    return datetime.now().astimezone()


def _origin_for(output_kind: OutputKind) -> str:
    """经历段 → 记忆 origin：内部段 internal，其余 external（Jshi_memory 210 映射）。"""
    return "internal" if output_kind is OutputKind.INTERNAL else "external"


def _name_for_object(objects: Mapping[str, str] | None, object_id: str | None) -> str:
    if not object_id:
        return ""
    for key, value in dict(objects or {}).items():
        name = str(key).strip()
        if name and str(value).strip() == object_id:
            return name
    return ""


def _speaker_name(segment, subject_name: str) -> str:
    if getattr(segment, "output_kind", None) is OutputKind.EXTERNAL_INPUT:
        return _name_for_object(
            getattr(segment, "objects", None),
            getattr(segment, "actor_object_id", None),
        )
    return subject_name


def _prefix_speaker(name: str, body: str) -> str:
    if not name:
        return body
    if body.startswith(f"{name}：") or body.startswith(f"{name}:"):
        return body
    return f"{name}：{body}"


def _embodied_from_state(state_delta: Mapping[str, object] | None) -> str:
    if not isinstance(state_delta, dict):
        return ""
    raw = state_delta.get("embodied")
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (list, tuple)):
        parts = [str(item).strip() for item in raw if str(item).strip()]
        return "；".join(parts)
    return ""


def _experience_text(segment, subject_name: str = "匠石") -> str:
    speaker = _speaker_name(segment, subject_name or "匠石")
    raw = getattr(segment, "text_raw", None)
    if raw:
        return _prefix_speaker(speaker, raw)
    embodied = _embodied_from_state(getattr(segment, "state_delta", None))
    if not embodied:
        return ""
    action = embodied if embodied.startswith("（动作：") else f"（动作：{embodied}）"
    return _prefix_speaker(speaker, action)


def _interlocutor_for(segment, subject_id: str) -> str | None:
    """本段对话对象：外部输入用说话人；回复段从映射表取，多人不任取。"""
    actor = getattr(segment, "actor_object_id", None)
    if actor:
        return actor
    peers = tuple(
        dict.fromkeys(
            value
            for raw in dict(getattr(segment, "objects", None) or {}).values()
            if (value := str(raw).strip()) and value != subject_id
        )
    )
    if len(peers) == 1:
        return peers[0]
    if len(peers) > 1:
        mentioned = getattr(segment, "mentioned_object_ids", ()) or ()
        for oid in mentioned:
            if oid in peers:
                return oid
        return None
    return None


def _window_minutes(raw: str) -> tuple[int, int]:
    start_raw, _, end_raw = raw.partition("-")
    def minutes(value: str) -> int:
        hour_text, _, minute_text = value.strip().partition(":")
        return int(hour_text) * 60 + int(minute_text or "0")

    return minutes(start_raw), minutes(end_raw)


@dataclass
class _ControlState:
    previous_status: str = "idle"
    attempts: int = 0
    last_error: str = ""
    last_flush_at: datetime | None = None


class InProcessMemoryControl(MemoryControlPort):
    """30 最小实现：冲刷策略（220）+ 批次构造 + 游标 / 重试 / 台账。

    触发参数全部可配置（默认见 Jshi_memory design/220）；后端 ingest_batch
    只负责接收（有序、失败隔离），行为不随策略变化。
    """

    def __init__(
        self,
        ledger: ExperienceLedgerPort,
        memory: MemoryBatchIngestPort,
        *,
        max_retry: int = 3,
        flush_max_chars: int | None = None,
        flush_max_segments: int = 20,
        flush_max_idle_seconds: float = 600,
        flush_on_idle_seconds: float = 1800,
        flush_night_window: str = "00:00-06:00",
        now: Callable[[], datetime] | None = None,
        subject_name: Callable[[str], str] | None = None,
    ) -> None:
        self._ledger = ledger
        self._memory = memory
        self._subject_name = subject_name
        self._max_retry = max_retry
        self._flush_max_chars = (
            flush_max_chars if flush_max_chars is not None else active_zone_chars()
        )
        self._flush_max_segments = flush_max_segments
        self._flush_max_idle_seconds = flush_max_idle_seconds
        self._flush_on_idle_seconds = flush_on_idle_seconds
        self._flush_night_window = flush_night_window
        self._now = now or local_now
        self._states: dict[str, _ControlState] = {}
        # 异步投递：subject_id -> (batch, entry, future, decision)。
        # 只由调用线程写入/读取；后台线程只 set future 结果，不碰账本与本状态。
        self._inflight: dict[str, tuple[Any, Any, Future, Any]] = {}

    # ------------------------------------------------------------------

    def _state(self, subject_id: str) -> _ControlState:
        state = self._states.get(subject_id)
        if state is None:
            state = _ControlState()
            self._states[subject_id] = state
        return state

    def _resolve_subject_name(self, subject_id: str) -> str:
        lookup = self._subject_name
        if lookup is None:
            return "匠石"
        try:
            name = lookup(subject_id)
        except KeyError:
            return "匠石"
        return str(name or "").strip() or "匠石"

    def _pending(self, subject_id: str):
        memory_start = self._ledger.consumer_cursor(subject_id, ConsumerKind.MEMORY)
        return self._ledger.list_experiences(subject_id, after_sequence=memory_start)

    def build_batch(self, subject_id: str) -> MemoryBatch | None:
        """取 memory_start 之后的未投递段，构造 MemoryBatch；无未投递段返回 None。"""
        pending = self._pending(subject_id)
        if not pending:
            return None
        speaker = self._resolve_subject_name(subject_id)
        experiences = tuple(
            MemoryExperience(
                subject_id=subject_id,
                text=_experience_text(segment, speaker),
                objects=dict(segment.objects or {}),
                interlocutor=_interlocutor_for(segment, subject_id),
                source_ids=tuple(segment.source_ids),
                occurred_at=segment.occurred_at,
                segment_id=segment.segment_id,
                origin=_origin_for(segment.output_kind),
            )
            for segment in pending
        )
        source_ids = tuple(
            dict.fromkeys(
                source_id
                for segment in pending
                for source_id in (segment.segment_id, *segment.source_ids)
            )
        )
        return MemoryBatch(
            batch_id=new_id(),
            subject_id=subject_id,
            experiences=experiences,
            from_sequence=pending[0].sequence,
            to_sequence=pending[-1].sequence,
            source_ids=source_ids,
        )

    # ------------------------------------------------------------------
    # 触发策略（Jshi_memory design/220）
    # ------------------------------------------------------------------

    def _flush_reason(self, subject_id: str, pending) -> str | None:
        now = self._now()
        state = self._state(subject_id)
        chars = sum(len(segment.text_raw or "") for segment in pending)
        if chars >= self._flush_max_chars:
            return "flush_max_chars"
        if len(pending) >= self._flush_max_segments:
            return "flush_max_segments"
        if state.last_flush_at is not None:
            idle_since_flush = (now - state.last_flush_at).total_seconds()
            if idle_since_flush >= self._flush_max_idle_seconds:
                return "flush_max_idle"
        newest = max(segment.occurred_at for segment in pending)
        if (now - newest).total_seconds() >= self._flush_on_idle_seconds:
            return "flush_on_idle"
        if self._in_night_window(now):
            return "flush_night_window"
        return None

    def _in_night_window(self, now: datetime) -> bool:
        raw = (self._flush_night_window or "").strip()
        if not raw or "-" not in raw:
            return False
        try:
            start, end = _window_minutes(raw)
        except ValueError:
            return False
        current = now.hour * 60 + now.minute
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end  # 跨午夜时段

    # ------------------------------------------------------------------

    def evaluate(self, subject_id: str) -> MemoryTriggerDecision:
        state = self._state(subject_id)
        if state.previous_status in {"pending", "ingesting"}:
            return MemoryTriggerDecision(
                should_trigger=False,
                reason="previous_memory_process_not_finished",
            )
        if state.previous_status == "failed" and state.attempts >= self._max_retry:
            return MemoryTriggerDecision(
                should_trigger=False,
                reason="max_retry_reached",
            )
        pending = self._pending(subject_id)
        if not pending:
            return MemoryTriggerDecision(
                should_trigger=False,
                reason="insufficient_memory_data",
            )
        reason = self._flush_reason(subject_id, pending)
        if reason is None:
            return MemoryTriggerDecision(
                should_trigger=False,
                reason="insufficient_memory_data",
            )
        return MemoryTriggerDecision(
            should_trigger=True,
            reason=reason,
            from_sequence=pending[0].sequence,
            to_sequence=pending[-1].sequence,
        )

    def run_once(self, subject_id: str) -> MemoryControlResult:
        decision = self.evaluate(subject_id)
        if not decision.should_trigger:
            return MemoryControlResult(decision=decision, status="skipped")
        batch = self.build_batch(subject_id)
        if batch is None:
            return MemoryControlResult(decision=decision, status="skipped")

        entry = self._ledger.register_ingest(batch)
        self._ledger.mark_ingesting(entry.ingest_id)
        state = self._state(subject_id)
        state.previous_status = "ingesting"
        try:
            result = self._memory.ingest_batch(batch)
            consumed = self._consumed_sequence(batch, result.stored_marks)
            self._ledger.advance_consumer_cursor(
                subject_id, ConsumerKind.MEMORY, through_sequence=consumed
            )
            self._ledger.mark_ingested(
                entry.ingest_id,
                memory_event_ids=result.sealed_event_ids,
                stored_marks=result.stored_marks,
            )
            state.previous_status = "idle"
            state.attempts = 0
            state.last_error = ""
            state.last_flush_at = self._now()
            return MemoryControlResult(
                decision=decision,
                status="ingested",
                ingest_id=entry.ingest_id,
            )
        except Exception as exc:  # 失败隔离：保留原文与批次，允许重试
            state.previous_status = "failed"
            state.attempts += 1
            state.last_error = str(exc)
            self._ledger.mark_failed(entry.ingest_id, reason=str(exc))
            return MemoryControlResult(
                decision=decision,
                status="failed",
                ingest_id=entry.ingest_id,
                error=str(exc),
            )

    def run_async(self, subject_id: str) -> MemoryControlResult:
        """异步投递：把重的 ingest_batch 移到后台线程，账本/游标/状态仍在当前线程收尾。"""
        self.drain(subject_id)
        decision = self.evaluate(subject_id)
        if not decision.should_trigger:
            return MemoryControlResult(decision=decision, status="skipped")
        batch = self.build_batch(subject_id)
        if batch is None:
            return MemoryControlResult(decision=decision, status="skipped")

        entry = self._ledger.register_ingest(batch)
        self._ledger.mark_ingesting(entry.ingest_id)
        state = self._state(subject_id)
        state.previous_status = "ingesting"

        future: Future = Future()
        self._inflight[subject_id] = (batch, entry, future, decision)

        def _work() -> None:
            try:
                future.set_result(self._memory.ingest_batch(batch))
            except Exception as exc:  # noqa: BLE001
                future.set_exception(exc)

        threading.Thread(
            target=_work,
            name=f"jshi-memory-{subject_id}",
            daemon=True,
        ).start()
        return MemoryControlResult(
            decision=decision,
            status="ingesting",
            ingest_id=entry.ingest_id,
        )

    def drain(self, subject_id: str) -> MemoryControlResult | None:
        """把上一批后台 ingest 的结果落回账本/游标（仍在当前线程）。"""
        inflight = self._inflight.get(subject_id)
        if inflight is None:
            return None
        batch, entry, future, decision = inflight
        if not future.done():
            return None
        del self._inflight[subject_id]

        state = self._state(subject_id)
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001
            state.previous_status = "failed"
            state.attempts += 1
            state.last_error = str(exc)
            self._ledger.mark_failed(entry.ingest_id, reason=str(exc))
            return MemoryControlResult(
                decision=decision,
                status="failed",
                ingest_id=entry.ingest_id,
                error=str(exc),
            )

        consumed = self._consumed_sequence(batch, result.stored_marks)
        self._ledger.advance_consumer_cursor(
            subject_id, ConsumerKind.MEMORY, through_sequence=consumed
        )
        self._ledger.mark_ingested(
            entry.ingest_id,
            memory_event_ids=result.sealed_event_ids,
            stored_marks=result.stored_marks,
        )
        state.previous_status = "idle"
        state.attempts = 0
        state.last_error = ""
        state.last_flush_at = self._now()
        return MemoryControlResult(
            decision=decision,
            status="ingested",
            ingest_id=entry.ingest_id,
        )

    def _consumed_sequence(self, batch: MemoryBatch, stored_marks) -> int:
        """凭 stored_marks 推进游标：推进到后端已接管的最远段。

        - 有 stored_marks：按封存段计算（未封存段内容已由后端缓冲持有，30 越过）；
        - 无 stored_marks（整批未封存）：推进到批次末端（后端已接管全部，等待闭环）。
        """
        sequences = [
            batch.from_sequence + index
            for index in range(len(batch.experiences))
        ]
        by_segment = {
            experience.segment_id: sequence
            for experience, sequence in zip(batch.experiences, sequences)
        }
        covered = [
            sequence
            for segment_id, sequence in by_segment.items()
            if segment_id in stored_marks
        ]
        return max(covered) if covered else batch.to_sequence

    def monitor(self, subject_id: str) -> MemoryProcessStatus:
        state = self._state(subject_id)
        memory_start = self._ledger.consumer_cursor(subject_id, ConsumerKind.MEMORY)
        pending = self._ledger.list_experiences(
            subject_id, after_sequence=memory_start
        )
        return MemoryProcessStatus(
            subject_id=subject_id,
            previous_status=state.previous_status,
            attempts=state.attempts,
            last_error=state.last_error,
            pending_sequences=tuple(segment.sequence for segment in pending),
        )
