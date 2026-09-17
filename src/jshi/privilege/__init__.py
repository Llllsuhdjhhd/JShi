"""超级权限对象与提示词规则写入。"""

from .rules import PromptRule, PromptRuleStore
from .superuser import SuperPermission, SuperPermissionStore

__all__ = [
    "PromptRule",
    "PromptRuleStore",
    "SuperPermission",
    "SuperPermissionStore",
]
