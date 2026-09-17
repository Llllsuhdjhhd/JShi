"""归属判断系统：输入到达后先判断归属到哪个已有主体面关切。占位系统。"""

from .port import AttributionPort, AttributionResult, PlaceholderAttribution

__all__ = ["AttributionPort", "AttributionResult", "PlaceholderAttribution"]
