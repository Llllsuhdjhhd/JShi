"""匠石侧定义模板目录（时间、异常等写在说明里，不改 Pi 协议）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

_DEFAULT = Path(__file__).resolve().parent / "templates" / "catalog.json"

_BUILTIN: tuple[Mapping[str, Any], ...] = (
    {
        "name": "generic",
        "intent_slot": "要用工具完成的意图",
        "time_note": "时长未知时写未知，不要编造精确秒数",
        "cost_note": "费用未知时留空",
        "result_note": "期望的结果形态，一句话",
        "exception_note": "可能失败、超时、无权限、工具不存在",
        "params": {},
        "expected_shape": "一句可核对的事实",
    },
    {
        "name": "echo",
        "intent_slot": "回显需求，仅测试",
        "time_note": "占位，约即时",
        "cost_note": "无费用",
        "result_note": "原文回显",
        "exception_note": "几乎不失败",
        "params": {},
        "expected_shape": "与 need 相同的短句",
    },
)


def load_catalog(path: str | Path | None = None) -> tuple[Mapping[str, Any], ...]:
    target = Path(path) if path is not None else _DEFAULT
    if not target.is_file():
        return _BUILTIN
    data = json.loads(target.read_text(encoding="utf-8"))
    items = data.get("templates") if isinstance(data, Mapping) else None
    if not isinstance(items, list):
        return ()
    return tuple(item for item in items if isinstance(item, Mapping) and item.get("name"))


def engine_tools(engine: Any) -> tuple[Mapping[str, Any], ...]:
    """引擎侧工具内容目录（name + description）。读不到、超时或抛错则空。"""
    try:
        listing = getattr(engine, "list_commands", None)
        if callable(listing):
            items = listing()
            return tuple(
                item
                for item in items
                if isinstance(item, Mapping) and str(item.get("name") or "").strip()
            )
        names = getattr(engine, "list_templates", None)
        if not callable(names):
            return ()
        return tuple({"name": name, "description": ""} for name in names())
    except Exception:
        return ()
