from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .profile import ObjectProfile, ObjectProfileRepository, new_object_id


# 置信度门禁阈值：候选置信度低于此值不进入后续活动。
MIN_OBJECT_CONFIDENCE = 0.5


@dataclass(frozen=True)
class SpeakerCandidate:
    """对象解析（身份识别）的输出：谁在说话 + 本次识别的可信程度。

    confidence 是每次活动动态计算的瞬时值，不落库。
    """

    label: str = "unknown"
    object_id: str | None = None
    confidence: float = 0.0
    status: str = "unknown"  # unknown | provisional | confirmed | rejected
    object_ref: str | None = None


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
    """基于对象档案的最小解析实现（占位级）。

    规则：渠道匹配优先（强信号）；再按显式名字匹配 label/别名；
    未命中 → 产生新对象候选（暂定，不立即落库，由主流程通过门禁后落库）。
    """

    name = "profile-recognition"

    def __init__(self, profiles: ObjectProfileRepository) -> None:
        self._profiles = profiles

    def resolve(
        self,
        subject_id: str,
        text: str,
        object_ref: str | None,
        channel: str | None = None,
    ) -> SpeakerCandidate:
        ref = (object_ref or "").strip()
        if channel:
            profile = self._profiles.find_by_channel(channel)
            if profile is not None:
                return _candidate(
                    profile,
                    ref,
                    0.95 if profile.status == "confirmed" else 0.70,
                )
        if ref:
            profile = self._profiles.get(ref) or self._profiles.find_by_name(ref)
            if profile is not None:
                return _candidate(
                    profile,
                    ref,
                    0.85 if profile.status == "confirmed" else 0.65,
                )
            # 显式名字未匹配 → 新对象候选（暂定，不落库）
            return SpeakerCandidate(
                label=ref,
                object_id=new_object_id(),
                confidence=0.60,
                status="provisional",
                object_ref=ref,
            )
        # 渠道给了但未匹配，或对象来源缺失 → unknown（低置信，被门禁阻断）
        return SpeakerCandidate(label="unknown", confidence=0.0, status="unknown")


def _candidate(
    profile: ObjectProfile, ref: str, base: float
) -> SpeakerCandidate:
    if profile.status == "rejected":
        return SpeakerCandidate(
            label=profile.label,
            object_id=profile.object_id,
            confidence=0.0,
            status="rejected",
            object_ref=ref,
        )
    return SpeakerCandidate(
        label=profile.label,
        object_id=profile.object_id,
        confidence=base,
        status=profile.status,
        object_ref=ref,
    )
