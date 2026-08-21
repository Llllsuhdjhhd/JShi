from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from .profile import (
    CarrierEntry,
    ObjectProfile,
    ObjectProfileRepository,
    new_object_id,
)


# 置信度门禁阈值：候选置信度低于此值不进入后续活动。
MIN_OBJECT_CONFIDENCE = 0.5

# 占位档位，与 doc/design/01-身份识别.md §4.1 一致。
CONF_BOUND_NEW = 0.95
CONF_BOUND_CONFIRMED = 1.00
CONF_CARRIER_NEW = 0.85
CONF_CARRIER_PROVISIONAL = 0.90
CONF_CARRIER_CONFIRMED = 0.98
CONF_NAME_NEW = 0.60
CONF_NAME_PROVISIONAL = 0.65
CONF_NAME_CONFIRMED = 0.85
CONF_MEMORY_PROVISIONAL = 0.55
CONF_MEMORY_CONFIRMED = 0.60


def _status_confidence(
    status: str,
    *,
    new: float,
    provisional: float,
    confirmed: float,
) -> float:
    if status == "confirmed":
        return confirmed
    if status == "provisional":
        return provisional
    return new


def bound_confidence(status: str | None = None) -> float:
    """文字通道已绑定：账号 / 会话 / object_id。"""
    if status == "confirmed":
        return CONF_BOUND_CONFIRMED
    return CONF_BOUND_NEW


def carrier_confidence(status: str | None = None) -> float:
    return _status_confidence(
        status or "",
        new=CONF_CARRIER_NEW,
        provisional=CONF_CARRIER_PROVISIONAL,
        confirmed=CONF_CARRIER_CONFIRMED,
    )


def name_confidence(status: str) -> float:
    return _status_confidence(
        status,
        new=CONF_NAME_NEW,
        provisional=CONF_NAME_PROVISIONAL,
        confirmed=CONF_NAME_CONFIRMED,
    )


def memory_confidence(status: str) -> float:
    if status == "confirmed":
        return CONF_MEMORY_CONFIRMED
    return CONF_MEMORY_PROVISIONAL


_WO_SKIP_SUFFIXES = frozenset(
    {
        "我们",
        "自我",
        "无我",
        "忘我",
        "本我",
        "超我",
        "大我",
        "小我",
        "物我",
        "人我",
        "一我",
        "真我",
        "假我",
    }
)


def normalize_text(
    text: str,
    *,
    speaker_id: str,
    subject_id: str,
    speaker_names: tuple[str, ...] = (),
    mentioned: Mapping[str, str] | None = None,
) -> str:
    """对象归一化：把文本中的「我」、说话人名字 / 称呼、已知提及替换为 id。

    外部输入：speaker_id = 说话人对象 id，「我」→ 说话人；
    主体回复 / 反思：speaker_id = subject_id，「我」→ 匠石。
    未识别或消歧失败的提及保持原文（合法状态）；原文由调用方另行保留。
    「我们 / 自我」等固定搭配不替换，避免误拆。
    """
    normalized = _replace_wo(text, speaker_id) if speaker_id else text
    replacements: list[tuple[str, str]] = [
        (name, speaker_id)
        for name in speaker_names
        if name and name.strip()
    ]
    for name, object_id in (mentioned or {}).items():
        if name and name.strip() and object_id:
            replacements.append((name, object_id))
    for name, object_id in sorted(
        replacements, key=lambda item: len(item[0]), reverse=True
    ):
        if name in normalized:
            normalized = normalized.replace(name, object_id)
    return normalized


def _replace_wo(text: str, speaker_id: str) -> str:
    """把独立「我」替换为 speaker_id；固定搭配（我们 / 自我…）不替换。"""
    if not text:
        return text
    parts: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "我" and text[index : index + 2] not in _WO_SKIP_SUFFIXES:
            parts.append(speaker_id)
            index += 1
        else:
            parts.append(text[index])
            index += 1
    return "".join(parts)


@dataclass(frozen=True)
class SpeakerCandidate:
    """对象解析（身份识别）的输出：谁在说话 + 本次识别的可信程度。

    confidence 是每次活动动态计算的瞬时值，不落库。
    有效候选必须可引用（object_id 必填）：无对象是输入信封契约错误，
    在入口校验阶段拒绝，不构成候选；"身份未确认"由 provisional 表达。
    `actor_object_id` 是外部对象 id；`subject_id` 是匠石主体 id，二者不可混用。
    名称与别名都必须带：label 非空；aliases 字段必有，首次可为空列表。
    """

    subject_id: str
    actor_object_id: str
    label: str = ""
    aliases: tuple[str, ...] = ()
    confidence: float = 0.0
    status: str = "provisional"  # provisional | confirmed | rejected
    object_ref: str | None = None
    mentioned_object_ids: tuple[str, ...] = ()
    carriers: tuple[CarrierEntry, ...] = ()
    reason: str = ""

    @property
    def object_id(self) -> str:
        """兼容旧调用：等价于 actor_object_id。"""
        return self.actor_object_id


class ObjectRecognitionPort(Protocol):
    """可替换的对象解析系统：输入通道携带的对象来源 + 文字层解析。"""

    def resolve(
        self,
        subject_id: str,
        text: str,
        object_ref: str | None,
        channel: str | None = None,
        carriers: tuple[CarrierEntry, ...] = (),
        mentioned_object_ids: tuple[str, ...] = (),
    ) -> SpeakerCandidate: ...


class ProfileObjectRecognition:
    """基于对象档案的占位解析实现：三模式识别链。

    模式一（载体识别）：输入携带 carriers 时按 kind+value 精确匹配，命中即唯一对象；
    模式二（文字标识）：先按 object_id 绑定，再按名字/别名匹配；唯一候选直接给对象；
    模式三（记忆匹配）：重名时用注入的 memory_matcher 消歧；不够领先则无法区分。
    未命中 → 产生新对象候选（暂定，不立即落库，由主流程通过门禁后落库）。
    """

    name = "profile-recognition"

    def __init__(
        self,
        profiles: ObjectProfileRepository,
        memory_matcher: Callable[
            [str, str, tuple[ObjectProfile, ...]],
            tuple[ObjectProfile, float] | None,
        ]
        | None = None,
    ) -> None:
        self._profiles = profiles
        self._memory_matcher = memory_matcher

    def resolve(
        self,
        subject_id: str,
        text: str,
        object_ref: str | None,
        channel: str | None = None,
        carriers: tuple[CarrierEntry, ...] = (),
        mentioned_object_ids: tuple[str, ...] = (),
    ) -> SpeakerCandidate:
        ref = (object_ref or "").strip()
        extra = {
            "mentioned_object_ids": mentioned_object_ids,
        }
        if carriers:
            for carrier in carriers:
                profile = self._profiles.find_by_carrier(carrier.kind, carrier.value)
                if profile is not None:
                    return _candidate(
                        subject_id,
                        profile,
                        ref or channel or carrier.value,
                        carrier_confidence(profile.status),
                        carriers=(carrier,),
                        reason="carrier_match",
                        **extra,
                    )
            label = ref or channel or carriers[0].value
            return _fresh(
                subject_id,
                label=label,
                confidence=CONF_CARRIER_NEW,
                object_ref=ref or channel,
                carriers=tuple(carriers),
                reason="carrier_unmatched",
                **extra,
            )
        if channel:
            profile = self._profiles.find_by_channel(channel)
            if profile is not None:
                return _candidate(
                    subject_id,
                    profile,
                    ref or channel,
                    bound_confidence(profile.status),
                    reason="channel_match",
                    **extra,
                )
            return _fresh(
                subject_id,
                label=ref or channel,
                confidence=CONF_BOUND_NEW,
                object_ref=ref or channel,
                reason="channel_unmatched",
                **extra,
            )
        if ref:
            by_id = self._profiles.get(ref)
            if by_id is not None:
                return _candidate(
                    subject_id,
                    by_id,
                    ref,
                    bound_confidence(by_id.status),
                    reason="object_id_match",
                    **extra,
                )
            candidates = self._profiles.find_by_names(ref)
            if len(candidates) == 1:
                profile = candidates[0]
                return _candidate(
                    subject_id,
                    profile,
                    ref,
                    name_confidence(profile.status),
                    reason="name_match",
                    **extra,
                )
            if len(candidates) > 1:
                return self._disambiguate(
                    subject_id, text, ref, candidates, extra
                )
            return _fresh(
                subject_id,
                label=ref,
                confidence=CONF_NAME_NEW,
                object_ref=ref,
                reason="new_name",
                **extra,
            )
        raise ValueError(
            "invalid input envelope: external input requires an object reference"
        )

    def _disambiguate(
        self,
        subject_id: str,
        text: str,
        ref: str,
        candidates: tuple[ObjectProfile, ...],
        extra: dict,
    ) -> SpeakerCandidate:
        if self._memory_matcher is not None:
            matched = self._memory_matcher(subject_id, text, candidates)
            if matched is not None:
                profile, score = matched
                return _candidate(
                    subject_id,
                    profile,
                    ref,
                    memory_confidence(profile.status),
                    reason=f"memory_match:{score:.2f}",
                    **extra,
                )
            return _fresh(
                subject_id,
                label=ref,
                confidence=0.0,
                object_ref=ref,
                reason="ambiguous_names_no_memory",
                **extra,
            )
        return _fresh(
            subject_id,
            label=ref,
            confidence=0.0,
            object_ref=ref,
            reason="ambiguous_names",
            **extra,
        )


def _fresh(
    subject_id: str,
    *,
    label: str,
    confidence: float,
    object_ref: str | None,
    reason: str,
    mentioned_object_ids: tuple[str, ...] = (),
    carriers: tuple[CarrierEntry, ...] = (),
) -> SpeakerCandidate:
    return SpeakerCandidate(
        subject_id=subject_id,
        actor_object_id=new_object_id(),
        label=label,
        aliases=(),
        confidence=confidence,
        status="provisional",
        object_ref=object_ref,
        mentioned_object_ids=mentioned_object_ids,
        carriers=carriers,
        reason=reason,
    )


def _candidate(
    subject_id: str,
    profile: ObjectProfile,
    ref: str,
    base: float,
    *,
    carriers: tuple[CarrierEntry, ...] = (),
    reason: str = "",
    mentioned_object_ids: tuple[str, ...] = (),
) -> SpeakerCandidate:
    if profile.status == "rejected":
        return SpeakerCandidate(
            subject_id=subject_id,
            actor_object_id=profile.object_id,
            label=profile.label,
            aliases=tuple(profile.aliases),
            confidence=0.0,
            status="rejected",
            object_ref=ref,
            mentioned_object_ids=mentioned_object_ids,
            carriers=carriers,
            reason=reason or "rejected_profile",
        )
    return SpeakerCandidate(
        subject_id=subject_id,
        actor_object_id=profile.object_id,
        label=profile.label,
        aliases=tuple(profile.aliases),
        confidence=base,
        status=profile.status,
        object_ref=ref,
        mentioned_object_ids=mentioned_object_ids,
        carriers=carriers,
        reason=reason,
    )
