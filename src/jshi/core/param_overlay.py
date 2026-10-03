"""参数覆盖层。进程启动时装入一次；读取函数只读这份当前快照。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from jshi.core.params import REGISTRY, ParamSpec


class OverlayError(Exception):
    """覆盖层不能用。文本里带路径和参数名，调用方不要捕获后改用默认值。"""

    def __init__(self, path: Path | str, name: str, reason: str) -> None:
        self.path = Path(path)
        self.name = name
        self.reason = reason
        super().__init__(f"{self.path}：参数 {name}：{reason}")


@dataclass(frozen=True)
class ParamSnapshot:
    version: int
    values: dict[str, Any]
    path: Path | None = None
    history: tuple[dict[str, Any], ...] = field(default_factory=tuple)


_current: ParamSnapshot | None = None
_pinned: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_value(path: Path | str, spec: ParamSpec, value: Any) -> None:
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise OverlayError(path, spec.name, "必须是布尔值")
        return
    if spec.kind == "int":
        if not _is_int(value):
            raise OverlayError(path, spec.name, "必须是整数")
    elif spec.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OverlayError(path, spec.name, "必须是数字")
    else:
        raise OverlayError(path, spec.name, "登记类型未知")
    if spec.low is not None and value < spec.low:
        raise OverlayError(path, spec.name, f"小于允许范围 {spec.low}")
    if spec.high is not None and value > spec.high:
        raise OverlayError(path, spec.name, f"大于允许范围 {spec.high}")


def validate_values(path: Path | str, values: Mapping[str, Any]) -> dict[str, Any]:
    """覆盖值按名称取代默认值。召回项、未登记项、locked 和越界都拒绝。"""
    cleaned: dict[str, Any] = {}
    for name, value in values.items():
        spec = REGISTRY.get(name)
        if spec is None:
            raise OverlayError(path, str(name), "未登记")
        if spec.store != "overlay":
            raise OverlayError(path, name, "不写入覆盖层")
        if spec.level == "locked":
            raise OverlayError(path, name, "locked，不许覆盖")
        _check_value(path, spec, value)
        cleaned[name] = value
    return cleaned


def current_snapshot() -> ParamSnapshot | None:
    return _current


def current_value(name: str, default: Any) -> Any:
    snapshot = _current
    if snapshot is None or name not in snapshot.values:
        return default
    return snapshot.values[name]


def load(path: Path | str) -> ParamSnapshot:
    file = Path(path)
    if not file.exists():
        return ParamSnapshot(version=0, values={}, path=file)
    try:
        raw = json_loads(file.read_text(encoding="utf-8") or "{}")
    except ValueError as exc:
        raise OverlayError(file, "", f"不是合法的 JSON（{exc}）") from exc
    if not isinstance(raw, dict):
        raise OverlayError(file, "", "文件内容必须是对象")
    values = raw.get("values") or {}
    if not isinstance(values, dict):
        raise OverlayError(file, "", "values 必须是对象")
    cleaned = validate_values(file, values)
    history = raw.get("history") or []
    if not isinstance(history, list):
        raise OverlayError(file, "", "history 必须是数组")
    return ParamSnapshot(
        version=int(raw.get("version") or 0),
        values=cleaned,
        path=file,
        history=tuple(item for item in history if isinstance(item, dict)),
    )


def json_loads(text: str) -> Any:
    import json

    return json.loads(text)


def _write_file(snapshot: ParamSnapshot) -> None:
    import json

    if snapshot.path is None:
        raise OverlayError("", "", "没有文件路径")
    snapshot.path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": snapshot.version,
        "values": snapshot.values,
        "history": list(snapshot.history),
    }
    snapshot.path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_overlay(
    path: Path | str,
    values: Mapping[str, Any],
    *,
    record_id: str = "",
) -> ParamSnapshot:
    """写入一整份覆盖。版本加一，历史保留上一份整表。本阶段对话路径不调用。"""
    file = Path(path)
    current = load(file) if file.exists() else ParamSnapshot(version=0, values={}, path=file)
    cleaned = validate_values(file, values)
    version = current.version + 1
    entry = {
        "version": version,
        "values": cleaned,
        "record_id": record_id,
        "at": _now(),
    }
    snapshot = ParamSnapshot(
        version=version,
        values=cleaned,
        path=file,
        history=(*current.history, entry),
    )
    _write_file(snapshot)
    return snapshot


def rollback(path: Path | str) -> ParamSnapshot:
    """把 values 换成上一版的整份内容，版本加一，不删历史。"""
    file = Path(path)
    if not file.exists():
        raise OverlayError(file, "", "文件不存在")
    import json

    try:
        raw = json.loads(file.read_text(encoding="utf-8") or "{}")
    except ValueError as exc:
        raise OverlayError(file, "", f"不是合法的 JSON（{exc}）") from exc
    if not isinstance(raw, dict):
        raise OverlayError(file, "", "文件内容必须是对象")
    version = int(raw.get("version") or 0)
    history = [item for item in (raw.get("history") or []) if isinstance(item, dict)]
    previous = [item for item in history if int(item.get("version") or 0) < version]
    if not previous:
        raise OverlayError(file, "", "没有上一版")
    prior = max(previous, key=lambda item: int(item.get("version") or 0))
    values = prior.get("values") or {}
    if not isinstance(values, dict):
        raise OverlayError(file, "", "上一版的 values 不是对象")
    cleaned = validate_values(file, values)
    new_version = version + 1
    entry = {
        "version": new_version,
        "values": cleaned,
        "record_id": "",
        "at": _now(),
    }
    snapshot = ParamSnapshot(
        version=new_version,
        values=cleaned,
        path=file,
        history=(*history, entry),
    )
    _write_file(snapshot)
    return snapshot


def install(path: Path | str) -> ParamSnapshot:
    """启动时调用一次。再次调用拒绝，测试请用上下文管理器。"""
    global _current, _pinned
    file = Path(path)
    if _pinned:
        raise OverlayError(file, "", "当前快照已安装，运行中不再更换")
    snapshot = load(file)
    _current = snapshot
    _pinned = True
    return snapshot


def ensure_installed(path: Path | str) -> ParamSnapshot:
    if _current is not None and _pinned:
        return _current
    return install(path)


def _clear() -> None:
    global _current, _pinned
    _current = None
    _pinned = False


@contextmanager
def using_overlay(path: Path | str) -> Iterator[ParamSnapshot]:
    """测试用：安装一份文件快照，退出后恢复到未安装。"""
    if _pinned:
        raise OverlayError(path, "", "当前快照已安装，运行中不再更换")
    snapshot = install(path)
    try:
        yield snapshot
    finally:
        _clear()


@contextmanager
def override_snapshot(values: Mapping[str, Any]) -> Iterator[ParamSnapshot]:
    """测试用：临时换上覆盖值，退出后恢复。不调用 install。"""
    global _current
    cleaned = validate_values("<snapshot>", values)
    previous = _current
    base = dict(previous.values) if previous is not None else {}
    base.update(cleaned)
    swapped = ParamSnapshot(
        version=previous.version if previous is not None else 0,
        values=base,
        path=previous.path if previous is not None else None,
        history=previous.history if previous is not None else (),
    )
    _current = swapped
    try:
        yield swapped
    finally:
        _current = previous
