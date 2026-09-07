"""匠石的能力实现（skill）框架：模型驱动的"结构化能力"。

对齐 I-004：skill / 模型 / 工具都是"能力实现"，按阶段按需选用；每个 skill 有版本、
来源、能力边界；每次活动记录实际使用的模型与 skill 版本。

主体：
- ``Skill``：能力基类（模型 + JSON schema + 解析 + 降级 + 版本）。
- ``SkillRegistry``：按能力名登记/选取。
- ``CognitionSkill``：05 认知 skill（首个子类）。

对象确认已是认知用途段，不另开 skill。反思等内部活动不套本 schema。评价与调参仍走旁路（I-005）。
"""

from .base import (
    Skill,
    SkillError,
    SkillModelPort,
    SkillRegistry,
    parse_json_object,
)
from .cognition import COGNITION_JSON_SCHEMA, CognitionSkill, salvage_persona_json
from .zone import WOOD_WRITE_INSTRUCTION, WOOD_WRITE_SCHEMA, WriteZoneSkill

__all__ = [
    "COGNITION_JSON_SCHEMA",
    "CognitionSkill",
    "Skill",
    "SkillError",
    "SkillModelPort",
    "SkillRegistry",
    "WriteZoneSkill",
    "parse_json_object",
    "salvage_persona_json",
]
