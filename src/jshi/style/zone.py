"""人格块级片场：按 subject_id 存「价值叙述 + 一组有序块」，落盘 JSON。

- 价值叙述（value）：boot 时单独生成，有独立字数上限（魔法数），作为片场开头 B1，
  不参与场景块的增删改，也不被预算裁掉。
- 场景块（blocks）：由模型的 edit（增删改）驱动，保留最有价值的；程序只忠实应用模型
  给出的 del/mod，不做「删开头」这类机械裁剪。
- 块号在每次渲染时按当前位置重排（B1..Bn）；模型只引用「当前这一份编号」。
- **每个块带时间（`at`）**：片场是不断重写的，写的时候都是「当下」；若不留时间，
  后接的块与先前的块就分不出先后，模型会拿隔日的旧结论当刚发生的事。渲染时按
  「相对现在」显示（`B3[3小时前] 我说：…`），精确时间仍存在这里。

约定：程序不自动从头部裁剪，预算由模型通过 edit 满足；若模型给不出符合预算的场景，
此处仍接受（预算为模型侧约束，不在此处硬裁）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import replace as _replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from jshi.models.prompt import relative_time_label

_LEADING_BLOCK_ID = re.compile(r"^B\d+\s+")
_LEADING_BLOCK_TIME = re.compile(r"^\[[^\]]{1,24}\]\s*")
_LEADING_BLOCK_REF = re.compile(r"^(?:B\d+)?(?:\[[^\]]{1,24}\])?\s*")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ZoneBlock:
    """片场里的一个块：正文 + 这个块是什么时候写下的。"""

    text: str
    at: datetime | None = None

    def label(self, *, now: datetime | None = None) -> str:
        """提示词里显示的相对时间标签（无时间则空）。"""
        return relative_time_label(self.at, now=now) if self.at else ""


def coerce_block(raw: object) -> ZoneBlock:
    """把落盘/调用方给的东西统一成 ``ZoneBlock``（兼容旧的裸字符串）。"""
    if isinstance(raw, ZoneBlock):
        return raw
    if isinstance(raw, Mapping):
        text = str(raw.get("text") or "").strip()
        stamp = raw.get("at") or raw.get("at_iso") or ""
        return ZoneBlock(text=text, at=_parse_at(stamp))
    return ZoneBlock(text=str(raw or "").strip(), at=None)


def _parse_at(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _clean_block_text(raw: object) -> str:
    """去掉模型误写在正文开头的块号（如「B5 lux说」）与渲染用的时间方括号。"""
    text = str(raw or "").strip()
    text = _LEADING_BLOCK_ID.sub("", text)
    text = _LEADING_BLOCK_TIME.sub("", text)
    return text.strip()


def _block_index(raw: object) -> int | None:
    """把 ``B2`` / ``b2`` / ``2`` 解析成 0-based 下标；解析不了返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    body = text[1:] if text[0].lower() == "b" else text
    try:
        n = int(body)
    except ValueError:
        return None
    return n - 1


class ZoneStore:
    """块级片场存储（内存 + 可选 JSON 落盘）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._zones: dict[str, tuple[str, tuple[ZoneBlock, ...]]] = {}
        if self.path is not None and self.path.is_file():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key, value in data.items():
                    if not isinstance(value, dict):
                        continue
                    narration = str(value.get("value", "") or "")
                    blocks = tuple(
                        block
                        for block in (coerce_block(item) for item in (value.get("blocks") or ()))
                        if block.text
                    )
                    self._zones[str(key)] = (narration, blocks)

    def _state(self, subject_id: str) -> tuple[str, tuple[ZoneBlock, ...]]:
        return self._zones.get(subject_id, ("", ()))

    def blocks(self, subject_id: str) -> tuple[ZoneBlock, ...]:
        """场景块（不含价值叙述），含各自的写入时间。"""
        return self._state(subject_id)[1]

    def get(self, subject_id: str) -> tuple[str, ...]:
        """只返回场景块正文（不含价值叙述、不含时间标签）。"""
        return tuple(block.text for block in self._state(subject_id)[1])

    def value_narration(self, subject_id: str) -> str:
        return self._state(subject_id)[0]

    def empty(self, subject_id: str) -> bool:
        value, blocks = self._state(subject_id)
        return not value and not blocks

    def body_chars(self, subject_id: str) -> int:
        """价值叙述 + 各场景块正文，不含 B 编号、不含时间标签。与提示词「片场总长」同一口径。"""
        value, blocks = self._state(subject_id)
        return len(value) + sum(len(block.text) for block in blocks)

    def _block_offset(self, subject_id: str) -> int:
        """渲染时价值叙述占 B1，场景块从 B2 起 → 场景块下标偏移 1。"""
        return 1 if self.value_narration(subject_id) else 0

    def save(
        self,
        subject_id: str,
        blocks: Iterable[object],
        *,
        at: datetime | None = None,
        keep_time: bool = True,
    ) -> tuple[str, ...]:
        """整份保存场景块。

        传进来的可以是 ``ZoneBlock``、``{"text","at"}`` 或裸字符串。裸字符串在
        ``keep_time=False`` 时按 ``at``（缺省现在）打时间戳；``keep_time=True`` 时
        保留原有块的时间（按正文原样匹配）。
        """
        value = self.value_narration(subject_id)
        previous = {block.text: block.at for block in self._state(subject_id)[1]}
        stamp = at or _utc_now()
        cleaned: list[ZoneBlock] = []
        for raw in blocks:
            block = coerce_block(raw)
            if not block.text:
                continue
            if block.at is None:
                if keep_time and block.text in previous:
                    block = _replace(block, at=previous[block.text])
                else:
                    block = _replace(block, at=stamp)
            cleaned.append(block)
        self._zones[subject_id] = (value, tuple(cleaned))
        self._flush()
        return tuple(block.text for block in cleaned)

    def boot(
        self,
        subject_id: str,
        value: str = "",
        scene: Iterable[object] = (),
        *,
        value_cap: int = 0,
        replace: bool = False,
    ) -> tuple[str, ...]:
        """一次性写场景：设价值叙述（按 value_cap 裁）+ 场景块。

        已有片场默认不覆盖（``replace=False``），避免续写轮误把现场整份抹掉。
        场景块不在此处按预算硬裁（预算由模型侧通过 edit 满足）。
        """
        if not replace and not self.empty(subject_id):
            return self.get(subject_id)
        narration = (value or "").strip()
        if value_cap and len(narration) > value_cap:
            narration = narration[:value_cap]
        stamp = _utc_now()
        blocks = tuple(
            _replace(block, at=block.at or stamp)
            for block in (coerce_block(item) for item in scene)
            if block.text
        )
        self._zones[subject_id] = (narration, blocks)
        self._flush()
        return tuple(block.text for block in blocks)

    def render(self, subject_id: str, *, now: datetime | None = None) -> str:
        """渲染片场：``B{n}[时间] 正文``。时间缺失的块不显示方括号。"""
        value, blocks = self._state(subject_id)
        rows: list[str] = []
        n = 0
        if value:
            n += 1
            rows.append(f"B{n}  {value}")
        for block in blocks:
            n += 1
            label = block.label(now=now)
            stamp = f"[{label}]" if label else ""
            rows.append(f"B{n}{stamp}  {block.text}")
        return "\n".join(rows)

    def apply_edit(
        self,
        subject_id: str,
        edits: Sequence[Mapping[str, object]],
        *,
        append_text: str = "",
        append_blocks: Sequence[str] = (),
        append_blocks_with_time: Sequence[ZoneBlock] = (),
    ) -> tuple[str, ...]:
        """按原始编号一次性应用模型给出的增/删/改，再追加程序块；不自动删开头。

        顺序：del/mod 已有块 → 模型 add → append_blocks → append_text。
        ``add`` 只应用 ``text``，忽略 ``id``。
        时间：`mod` **保留原块时间**（内容改了，但「什么时候知道的」不该刷新）；
        `add` 与程序追加的块用当前时间。
        """
        current = list(self.blocks(subject_id))
        offset = self._block_offset(subject_id)
        del_indices: set[int] = set()
        mods: dict[int, str] = {}
        add_blocks: list[str] = []
        for edit in edits or ():
            if not isinstance(edit, Mapping):
                continue
            op = str(edit.get("op") or "").strip()
            if op == "add":
                text = _clean_block_text(edit.get("text"))
                if text:
                    add_blocks.append(text)
                continue
            index = _block_index(edit.get("id"))
            if index is None:
                continue
            scene_index = index - offset
            if scene_index < 0 or scene_index >= len(current):
                continue  # 引用价值叙述(B1)或不存在块 → 忽略
            if op == "del":
                del_indices.add(scene_index)
            elif op == "mod":
                text = _clean_block_text(edit.get("text"))
                if text:
                    mods[scene_index] = text
        stamp = _utc_now()
        result: list[ZoneBlock] = []
        for i, block in enumerate(current):
            if i in del_indices:
                continue
            if i in mods:
                result.append(ZoneBlock(text=mods[i], at=block.at or stamp))
            else:
                result.append(block)
        result.extend(ZoneBlock(text=text, at=stamp) for text in add_blocks)
        result.extend(append_blocks_with_time or ())
        extra = [*(append_blocks or ()), append_text]
        for text in extra:
            cleaned = str(text or "").strip()
            if cleaned:
                result.append(ZoneBlock(text=cleaned, at=stamp))
        return self.save(subject_id, result, keep_time=False)

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {
                "value": value,
                "blocks": [
                    {"text": block.text, "at": block.at.isoformat() if block.at else ""}
                    for block in blocks
                ],
            }
            for key, (value, blocks) in self._zones.items()
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
