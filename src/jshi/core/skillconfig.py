"""Skill 模型调用配置：每个 skill 独立 模型/思考/流式/版本，中心统一管理。

对齐 I-004：每个 skill 是独立能力实现，各有模型与版本（``model_tag``）。
本模块只负责「按 skill 选模型与思考档位」，不涉及主流程编排。

- ``SkillProfile``：一份 skill 的模型调用配置。
- ``SkillConfigStore``：按 skill 名管理；未配置时回退到全局环境（``JSHI_MODEL_*`` /
  ``JSHI_MODEL_THINKING``），保持既有行为。
- ``build_model_port``：按 profile 造底层 ``ModelPort``（含 per-skill thinking）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Mapping

THINKING_DEFAULT = "disabled"

# 每个 skill 可用的环境变量字段（`.env` 风格，非秘密）；API 凭据按厂商走 JSHI_API_<厂商>_*。
SKILL_ENV_FIELDS = (
    "MODEL",
    "VENDOR",
    "THINKING",
    "REASONING_EFFORT",
    "MAX_TOKENS",
    "RESPONSE_FORMAT",
    "STREAMING",
    "VERSION",
)


def skill_env_var(name: str, field: str) -> str:
    """per-skill 环境变量名：``JSHI_SKILL_<NAME>_<FIELD>``。"""
    return f"JSHI_SKILL_{name.upper()}_{field.upper()}"


def _env_bool(raw: str | None, *, default: bool = True) -> bool:
    """解析环境变量布尔值；空/缺省用 ``default``。"""
    value = str(raw or "").strip().lower()
    if value in {"", "true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    return default


def _env_int(raw: str | None) -> int | None:
    """解析环境变量整数；空/非法返回 None。"""
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


# 兼容旧单值（disabled/low/high/max）到 thinking+reasoning_effort 的拆分。
def _split_thinking(raw: str | None) -> tuple[str, str]:
    """把旧的组合思考值拆成 ``(thinking, reasoning_effort)``。

    - ``disabled/off/...`` / 未设 → 思考关（effort 用默认 high，无意义）。
    - ``low`` → thinking=enabled, effort=low；``max`` → enabled, max；其余开思考默认 high。
    """
    value = str(raw or "").strip().lower()
    if value in {"disabled", "off", "0", "false", "no"}:
        return "disabled", "high"
    if value == "low":
        return "enabled", "low"
    if value == "max":
        return "enabled", "max"
    if value in {"enabled", "on", "1", "true", "yes", "high", "medium", "xhigh"}:
        return "enabled", "high"
    return "disabled", "high"


@dataclass(frozen=True)
class SkillProfile:
    """一个 skill 的模型调用配置。

    - ``thinking``：enabled / disabled（要不要思考）。
    - ``reasoning_effort``：low / high / max（思考强度；``medium``/``xhigh`` 映射 high）。
    - ``max_tokens``：可选单次生成长度上限。
    - ``response_format``：``text`` / ``json_object``（结构化 skill 可设 json_object）。
    - ``streaming``：该 skill 是否可流式（不流式时回复仍回落 ``generate``）。
    - ``version``：可选覆盖该 skill 版本（缺省用 skill 类自身默认）。
    """

    name: str
    model: str = ""
    # 厂商名（如 ``deepseek`` / ``openai``）：程序按 ``JSHI_API_<厂商>_ENDPOINT/API_KEY`` 从 env 取凭据。
    vendor: str = ""
    endpoint: str = ""
    api_key: str = ""
    thinking: str = THINKING_DEFAULT
    reasoning_effort: str = "high"
    max_tokens: int | None = None
    response_format: str = ""
    streaming: bool = True
    version: str = ""

    @property
    def external(self) -> bool:
        return bool(self.endpoint and self.api_key and self.model)


class SkillConfigStore:
    """按 skill 名管理 ``SkillProfile``；缺省回退到全局环境。"""

    def __init__(
        self,
        profiles: Iterable[SkillProfile] | None = None,
        *,
        default_loading: bool = True,
    ) -> None:
        self._profiles: dict[str, SkillProfile] = {}
        self._default_loading = default_loading
        for profile in profiles or ():
            self.set(profile)

    def set(self, profile: SkillProfile) -> SkillProfile:
        self._profiles[profile.name] = profile
        return profile

    def get(self, name: str) -> SkillProfile:
        return self._profiles.get(name) or self.default_profile(name)

    def profile(self, name: str) -> SkillProfile:
        """同 ``get``，语义化命名。"""
        return self.get(name)

    def default_profile(self, name: str) -> SkillProfile:
        """回退配置：模型取 ``JSHI_MODEL_*``，厂商可选 ``JSHI_SKILL_<NAME>_VENDOR``。

        ``endpoint``/``api_key`` 留空，交由 ``build_model_port`` 按 ``vendor`` 从 env 取
        （``JSHI_API_<厂商>_*``），否则回落全局 ``JSHI_MODEL_*``。
        思考：旧 ``JSHI_MODEL_THINKING``（disabled/low/high/max）经 ``_split_thinking`` 拆成
        thinking + reasoning_effort；per-skill ``JSHI_SKILL_<NAME>_THINKING/REASONING_EFFORT``
        可单独覆盖。
        """
        prefix = f"JSHI_SKILL_{name.upper()}_"

        def _get(field: str, fallback: str) -> str:
            return os.getenv(prefix + field, fallback)

        model = _get("MODEL", os.getenv("JSHI_MODEL_NAME", ""))
        vendor = _get("VENDOR", "").strip()
        thinking, effort = _split_thinking(
            _get("THINKING", os.getenv("JSHI_MODEL_THINKING", ""))
        )
        reasoning_effort = _get("REASONING_EFFORT", "").strip() or effort
        max_tokens = _env_int(_get("MAX_TOKENS", ""))
        response_format = _get("RESPONSE_FORMAT", "").strip()
        streaming = _env_bool(_get("STREAMING", ""), default=True)
        version = _get("VERSION", "").strip()
        return SkillProfile(
            name=name,
            model=model,
            vendor=vendor,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            response_format=response_format,
            streaming=streaming,
            version=version,
        )

    def load_mapping(self, data: Mapping[str, Mapping[str, object]]) -> None:
        """批量装载 ``{skill: {model, vendor, thinking, reasoning_effort, max_tokens, response_format, streaming, version}}``。

        供配置文件/集中管理使用；**未给或为空的字段沿用 ``default_profile``（回退 env）**——
        这样 ``skills.json`` 里写 ``"model": ""`` 即表示"用 env 的模型"。
        """
        for name, raw in data.items():
            base = self.default_profile(str(name))
            overrides = {
                key: value
                for key, value in dict(raw).items()
                if value not in (None, "")
            }
            merged = {**base.__dict__, **overrides}
            merged["name"] = str(name)
            self.set(SkillProfile(**merged))

    def load_file(self, path: str | os.PathLike) -> None:
        """从 JSON 配置文件装载（``{skill: {...}}`` 或 ``{"skills": {...}}``）。

        文件存在与否由调用方判断；本方法只装载。字段缺省回退 env（见 ``load_mapping``）。
        """
        import json
        from pathlib import Path

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        payload = data.get("skills") if isinstance(data, dict) and "skills" in data else data
        if isinstance(payload, Mapping):
            self.load_mapping(payload)

    def names(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def env_template(self, name: str) -> str:
        """渲染一段 `.env` 风格模板（当前值），供用户复制编辑。

        例如 ``JSHI_SKILL_COGNITION_THINKING=high``。
        """
        profile = self.profile(name)
        map_ = {
            "MODEL": profile.model,
            "VENDOR": profile.vendor,
            "THINKING": profile.thinking,
            "REASONING_EFFORT": profile.reasoning_effort,
            "MAX_TOKENS": (str(profile.max_tokens) if profile.max_tokens is not None else ""),
            "RESPONSE_FORMAT": profile.response_format,
            "STREAMING": str(profile.streaming).lower(),
            "VERSION": profile.version,
        }
        return "\n".join(f"{skill_env_var(name, field)}={map_[field]}" for field in SKILL_ENV_FIELDS)


def _vendor_env(profile: SkillProfile, field: str) -> str:
    """按厂商取 env：``JSHI_API_<厂商>_<field>``。未设厂商返回空。"""
    if not profile.vendor:
        return ""
    return os.getenv(f"JSHI_API_{profile.vendor.upper()}_{field}", "")


def build_model_port(profile: SkillProfile) -> "ModelPort":
    """按 profile 造底层模型端口。

    凭据解析优先级：``profile.endpoint/api_key``（显式） → 厂商 env
    （``JSHI_API_<厂商>_ENDPOINT/API_KEY``） → 全局 ``JSHI_MODEL_*``。
    三者齐备且 ``profile.model`` 有值才造 ``OpenAICompatibleModel``；否则退 ``EchoModel``。

    延迟导入，避免与 ``jshi.models.base`` 的循环引用。
    """
    from jshi.models.base import EchoModel, ModelPort, OpenAICompatibleModel

    endpoint = (
        profile.endpoint
        or _vendor_env(profile, "ENDPOINT")
        or os.getenv("JSHI_MODEL_ENDPOINT", "")
    )
    api_key = (
        profile.api_key
        or _vendor_env(profile, "API_KEY")
        or os.getenv("JSHI_MODEL_API_KEY", "")
    )
    model = profile.model
    if endpoint and api_key and model:
        return OpenAICompatibleModel(
            endpoint,
            api_key,
            model,
            thinking=profile.thinking,
            reasoning_effort=profile.reasoning_effort,
            max_tokens=profile.max_tokens,
            response_format=profile.response_format,
        )
    # 无外部凭据 → 占位 EchoModel（无外部模型时思考档位无意义）。
    return EchoModel()
