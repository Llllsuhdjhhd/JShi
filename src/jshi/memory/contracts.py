from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Sequence, TYPE_CHECKING
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


if TYPE_CHECKING:
    from .port import RecalledFragment


@dataclass(frozen=True)
class RecallRequestContext:
    subject_id: str
    activity_id: str
    session_id: str
    query_text: str
    object_ids: tuple[str, ...] = ()
    interlocutor_object_id: str | None = None
    level: int = 1
    candidate_limit: int = 12
    enabled: bool = True
    input_revision: str = ""
    run_id: str = field(default_factory=new_id)
    created_at: float = 0.0
    policy_version: str = "recall-flow-v1"
    mode: int = 2
    selected_limit: int | None = None


@dataclass(frozen=True)
class RecallCandidateBatch:
    request: RecallRequestContext
    candidates: tuple[RecalledFragment, ...] = ()
    reused_ids: tuple[str, ...] = ()
    status: str = "completed"
    error: str = ""
    elapsed_ms: float = 0.0
    base_version: int = 0
    query_runs: tuple[Mapping[str, object], ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    truncated: bool = False
    invalidated_ids: tuple[str, ...] = ()  # Source invalidations survive candidate budgets.


@dataclass(frozen=True)
class MemoryExperience:
    """一段经历：30 按经历段映射构造，后端 ingest 的输入单元（对齐 Jshi_memory 210）。

    - ``text`` 是对话行（说话人名字 + 言语/动作）；无言语无动作则为空，不用 JSON 冒充；
    - ``objects`` 覆盖一段经历涉及的所有对象（名字/称呼 → 01 object_id），与是否说话无关；
      可为空 = 主体记忆；
    - ``segment_id`` 是 30 侧经历段 id，用于 ``stored_marks`` 回填与游标推进。
    """

    subject_id: str
    text: str
    objects: Mapping[str, str] = field(default_factory=dict)
    source_ids: tuple[str, ...] = ()
    occurred_at: datetime | None = None
    segment_id: str | None = None
    origin: str = "external"  # external | internal | dream
    # 说话/互动对象 id（本段主体对话的对象；区别于 objects 的泛提及）。
    # None = 主体自述/系统段。用于回忆时按说话人软纠偏（design/1010 防串线）。
    interlocutor: str | None = None
    # Raw media stays in the visual store; the backend receives references and evidence.
    media: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class MemoryBatch:
    """30 构造的投递批次：subject_id + experiences（有序 = 经历先后）。

    ``from_sequence`` / ``to_sequence`` 是 30 侧台账字段（后端端口形状以 210 为准，
    只消费 subject_id + experiences）。
    """

    batch_id: str
    subject_id: str
    experiences: tuple[MemoryExperience, ...]
    from_sequence: int
    to_sequence: int
    source_ids: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class BackendIngestResult:
    """记忆写入结果（后端 → 30）：``stored_marks`` 供游标推进与台账。"""

    subject_id: str
    stored_marks: Mapping[str, Sequence[str]] = field(default_factory=dict)
    sealed_event_ids: tuple[str, ...] = ()
    role_ids: tuple[str, ...] = ()  # 本轮登记/更新的 01 对象，可为空
    unclosed_count: int = 0
    errors: tuple[str, ...] = ()
    # None: retain media with accepted experiences. Explicit mapping selects media per segment.
    media_marks: Mapping[str, Sequence[str]] | None = None


__all__ = [
    "RecallRequestContext",
    "RecallCandidateBatch",
    "BackendIngestResult",
    "MemoryBatch",
    "MemoryExperience",
    "new_id",
    "utc_now",
]
