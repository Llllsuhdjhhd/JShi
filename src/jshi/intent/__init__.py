"""意图系统：记录意图（继续/暂停/完成/放弃）。未填充，占位系统。"""

from .port import IntentPort, PlaceholderIntent

__all__ = ["IntentPort", "PlaceholderIntent"]
