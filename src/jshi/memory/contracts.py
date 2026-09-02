from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Sequence
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


@dataclass(frozen=True)
class MemoryExperience:
    """一段经历：30 按经历段映射构造，后端 ingest 的输入单元（对齐 Jshi_memory 210）。

    - ``text`` 是原文，不归一化、不改写、不切分；state 类段以 JSON 表示；
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


__all__ = [
    "BackendIngestResult",
    "MemoryBatch",
    "MemoryExperience",
    "new_id",
    "utc_now",
]
