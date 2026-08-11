from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .profile import (
    CarrierEntry,
    ObjectProfile,
    ObjectProfileRepository,
    new_object_id,
)


# 置信度门禁阈值：候选置信度低于此值不进入后续活动。
MIN_OBJECT_CONFIDENCE = 0.5


@dataclass(frozen=True)
class SpeakerCandidate:
    """对象解析（身份识别）的输出：谁在说话 + 本次识别的可信程度。

    confidence 是每次活动动态计算的瞬时值，不落库。
    有效候选必须可引用（object_id 必填）：无对象是输入信封契约错误，
    在入口校验阶段拒绝，不构成候选；"身份未确认"由 provisional 表达。
    """

    object_id: str
    label: str = ""
    confidence: float = 0.0
    status: str = "provisional"  # provisional | confirmed | rejected
    object_ref: str | None = None
    carriers: tuple[CarrierEntry, ...] = ()
    reason: str = ""


class ObjectRecognitionPort(Protocol):
    """可替换的对象解析系统：输入通道携带的对象来源 + 文字层解析。"""

    def resolve(
        self,
        subject_id: str,
        text: str,
        object_ref: str | None,
        channel: str | None = None,
    ) -> SpeakerCandidate: ...


class ProfileObjectRecognition:
    """基于对象档案的占位解析实现：三模式识别链。

    模式一（载体识别）：输入携带 carriers 时按 kind+value 精确匹配，命中即唯一对象；
    模式二（文字标识）：按显式名字匹配 label/别名得到候选集，唯一候选直接给对象；
    模式三（记忆匹配）：重名时用注入的 memory_matcher 按候选对象记忆消歧，给置信最高者。
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
    ) -> SpeakerCandidate:
        ref = (object_ref or "").strip()
        if carriers:
            for carrier in carriers:
                profile = self._profiles.find_by_carrier(carrier.kind, carrier.value)
                if profile is not None:
                    return _candidate(
                        profile,
                        ref or channel or carrier.value,
                        0.95 if profile.status == "confirmed" else 0.70,
                        carriers=(carrier,),
                        reason="carrier_match",
                    )
            # 载体引用未匹配 → 以载体为引用产生暂定候选（携带 carriers 供落库）
            return SpeakerCandidate(
                object_id=new_object_id(),
                label=ref or channel or carriers[0].value,
                confidence=0.70,
                status="provisional",
                object_ref=ref or channel,
                carriers=tuple(carriers),
                reason="carrier_unmatched",
            )
        if channel:
            profile = self._profiles.find_by_channel(channel)
            if profile is not None:
                return _candidate(
                    profile,
                    ref or channel,
                    0.95 if profile.status == "confirmed" else 0.70,
                    reason="channel_match",
                )
            # 渠道提供引用但未匹配已确认档案 → 暂定对象候选（可引用标识）
            return SpeakerCandidate(
                object_id=new_object_id(),
                label=ref or channel,
                confidence=0.70,
                status="provisional",
                object_ref=ref or channel,
                reason="channel_unmatched",
            )
        if ref:
            candidates = self._profiles.find_by_names(ref)
            if len(candidates) == 1:
                profile = candidates[0]
                return _candidate(
                    profile,
                    ref,
                    0.85 if profile.status == "confirmed" else 0.65,
                    reason="name_match",
                )
            if len(candidates) > 1 and self._memory_matcher is not None:
                matched = self._memory_matcher(subject_id, text, candidates)
                if matched is not None:
                    profile, score = matched
                    return _candidate(
                        profile,
                        ref,
                        0.60 if profile.status == "confirmed" else 0.55,
                        reason=f"memory_match:{score:.2f}",
                    )
                return SpeakerCandidate(
                    object_id=new_object_id(),
                    label=ref,
                    confidence=0.0,
                    status="provisional",
                    object_ref=ref,
                    reason="ambiguous_names_no_memory",
                )
            if len(candidates) > 1:
                return SpeakerCandidate(
                    object_id=new_object_id(),
                    label=ref,
                    confidence=0.0,
                    status="provisional",
                    object_ref=ref,
                    reason="ambiguous_names",
                )
            # 显式名字未匹配 → 新对象候选（暂定，不落库）
            return SpeakerCandidate(
                object_id=new_object_id(),
                label=ref,
                confidence=0.60,
                status="provisional",
                object_ref=ref,
                reason="new_name",
            )
        # 两者皆无 → 无效输入信封（正常由主流程入口校验拒绝；此处兜底）
        raise ValueError(
            "invalid input envelope: external input requires an object reference"
        )


def _candidate(
    profile: ObjectProfile,
    ref: str,
    base: float,
    *,
    carriers: tuple[CarrierEntry, ...] = (),
    reason: str = "",
) -> SpeakerCandidate:
    if profile.status == "rejected":
        return SpeakerCandidate(
            object_id=profile.object_id,
            label=profile.label,
            confidence=0.0,
            status="rejected",
            object_ref=ref,
            carriers=carriers,
            reason=reason or "rejected_profile",
        )
    return SpeakerCandidate(
        object_id=profile.object_id,
        label=profile.label,
        confidence=base,
        status=profile.status,
        object_ref=ref,
        carriers=carriers,
        reason=reason,
    )
