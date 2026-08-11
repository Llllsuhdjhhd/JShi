"""身份识别（对象解析）：输入边界确定外部输入来自谁。占位系统。"""

from .port import (
    MIN_OBJECT_CONFIDENCE,
    ObjectRecognitionPort,
    ProfileObjectRecognition,
    SpeakerCandidate,
)
from .profile import CarrierEntry, ObjectProfile, ObjectProfileRepository, new_object_id

__all__ = [
    "MIN_OBJECT_CONFIDENCE",
    "CarrierEntry",
    "ObjectProfile",
    "ObjectProfileRepository",
    "ObjectRecognitionPort",
    "ProfileObjectRecognition",
    "SpeakerCandidate",
    "new_object_id",
]
