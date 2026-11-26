from dataclasses import dataclass
import os

MLX_MODEL = 'mlx-community/Qwen3.6-35B-A3B-4bit'
MLX_ENDPOINT = 'http://127.0.0.1:18765/v1'


@dataclass(frozen=True)
class VisionConfig:
    enabled: bool = False
    endpoint: str = MLX_ENDPOINT
    api_key: str = ""
    model: str = MLX_MODEL
    detector: str = "change"
    jev_images: bool = False
    cognition_images: bool = False  # Enable only for a main model accepting image input.
    weights: str = "yolo11n.pt"
    sample_seconds: float = 2.0
    refresh_strategy: str = 'auto'
    local_refresh_seconds: float = 2.0
    archive_seconds: float = 10
    cooldown_seconds: float = 15
    refresh_seconds: float = 300
    rolling_bytes: int = 256 * 1024 * 1024
    memory_bytes: int = 512 * 1024 * 1024
    retention_seconds: float = 86400
    overview_side: int = 512
    recall_photo_limit: int = 4
    main_image_bytes: int = 8 * 1024 * 1024
    recent_scene_seconds: int = 30
    max_image_bytes: int = 8 * 1024 * 1024
    timeout_seconds: float = 45
    max_calls_per_hour: int = 120
    face_enabled: bool = False
    face_detector_model: str = ""
    face_recognizer_model: str = ""
    portrait_enabled: bool = True
    portrait_limit: int = 8
    face_identity_limit: int = 128
    face_match_threshold: float = .50
    face_match_gap: float = .08
    face_min_size: int = 64
    portrait_similarity_limit: float = .95
    cloud_enabled: bool = True

    def __post_init__(self):
        if self.refresh_strategy not in {'auto', 'change'}:
            raise ValueError('vision refresh strategy must be auto or change')
        if self.detector not in {"change", "yolo"}:
            raise ValueError("vision detector must be change or yolo")
        for name in ("sample_seconds", "local_refresh_seconds", "archive_seconds", "cooldown_seconds", "refresh_seconds",
                     "rolling_bytes", "memory_bytes", "retention_seconds", "overview_side", "main_image_bytes", "recall_photo_limit", "recent_scene_seconds", "max_image_bytes", "timeout_seconds", "max_calls_per_hour", "portrait_limit", "face_min_size", "face_identity_limit"):
            if getattr(self, name) <= 0:
                raise ValueError(f"vision {name} must be positive")
        if not 0 < self.face_match_threshold < 1 or not 0 <= self.face_match_gap < 1 or not 0 < self.portrait_similarity_limit <= 1:
            raise ValueError('invalid face matching thresholds')

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
        # Preserve explicitly configured cloud deployments and their legacy defaults.
        if values.get('model', MLX_MODEL) != MLX_MODEL:
            values.setdefault('endpoint', os.getenv('JSHI_API_DEEPSEEK_ENDPOINT') or 'https://api.deepseek.com/chat/completions')
        elif 'endpoint' in values and 'model' not in values and not cls(endpoint=values['endpoint']).local_mlx:
            values['model'] = 'deepseek-flash'
        return cls(**values)

    @property
    def local_mlx(self):
        from urllib.parse import urlsplit
        return self.model == MLX_MODEL and urlsplit(self.endpoint).hostname in {'127.0.0.1', 'localhost', '::1'}
