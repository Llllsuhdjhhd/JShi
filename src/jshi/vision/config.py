from dataclasses import dataclass
import os


@dataclass(frozen=True)
class VisionConfig:
    enabled: bool = False
    endpoint: str = "https://api.deepseek.com/chat/completions"
    api_key: str = ""
    model: str = "deepseek-flash"
    detector: str = "change"
    jev_images: bool = False
    weights: str = "yolo11n.pt"
    sample_seconds: float = 2
    archive_seconds: float = 10
    cooldown_seconds: float = 15
    refresh_seconds: float = 300
    rolling_bytes: int = 256 * 1024 * 1024
    memory_bytes: int = 512 * 1024 * 1024
    retention_seconds: float = 86400
    max_image_bytes: int = 8 * 1024 * 1024
    timeout_seconds: float = 45
    max_calls_per_hour: int = 120

    def __post_init__(self):
        if self.detector not in {"change", "yolo"}:
            raise ValueError("vision detector must be change or yolo")
        for name in ("sample_seconds", "archive_seconds", "cooldown_seconds", "refresh_seconds",
                     "rolling_bytes", "memory_bytes", "retention_seconds", "max_image_bytes", "timeout_seconds", "max_calls_per_hour"):
            if getattr(self, name) <= 0:
                raise ValueError(f"vision {name} must be positive")

    @classmethod
    def from_env(cls):
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            raw = os.getenv("JSHI_VISION_" + name.upper())
            if raw is not None:
                base = getattr(defaults, name)
                values[name] = raw.lower() in {"1", "true", "yes", "on"} if isinstance(base, bool) else type(base)(raw)
        if not values.get("api_key"):
            values["api_key"] = os.getenv("JSHI_API_DEEPSEEK_API_KEY", "")
        endpoint = os.getenv("JSHI_API_DEEPSEEK_ENDPOINT", "")
        if endpoint and "endpoint" not in values:
            values["endpoint"] = endpoint
        return cls(**values)
