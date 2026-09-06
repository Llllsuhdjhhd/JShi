"""人格块级片场：按 subject_id 存「价值叙述 + 一组有序块」，落盘 JSON。

- 价值叙述（value）：boot 时单独生成，有独立字数上限（魔法数），作为片场开头 B1，
  不参与场景块的增删改，也不被预算裁掉。
- 场景块（blocks）：由模型的 edit（增删改）驱动，保留最有价值的；程序只忠实应用模型
  给出的 del/mod，不做「删开头」这类机械裁剪。
- 块号在每次渲染时按当前位置重排（B1..Bn）；模型只引用「当前这一份编号」。

约定：程序不自动从头部裁剪，预算由模型通过 edit 满足；若模型给不出符合预算的场景，
此处仍接受（预算为模型侧约束，不在此处硬裁）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Mapping, Sequence

_LEADING_BLOCK_ID = re.compile(r"^B\d+\s+")


def _clean_block_text(raw: object) -> str:
    """去掉模型误写在正文开头的块号（如「B5 lux说」）。"""
    return _LEADING_BLOCK_ID.sub("", str(raw or "").strip()).strip()


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
        self._zones: dict[str, tuple[str, tuple[str, ...]]] = {}
        if self.path is not None and self.path.is_file():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key, value in data.items():
                    if not isinstance(value, dict):
                        continue
                    narration = str(value.get("value", "") or "")
                    blocks = tuple(
                        str(item).strip()
                        for item in (value.get("blocks") or ())
                        if str(item).strip()
                    )
                    self._zones[str(key)] = (narration, blocks)

    def _state(self, subject_id: str) -> tuple[str, tuple[str, ...]]:
        return self._zones.get(subject_id, ("", ()))

    def get(self, subject_id: str) -> tuple[str, ...]:
        """只返回场景块（不含价值叙述）。"""
        return self._state(subject_id)[1]

    def value_narration(self, subject_id: str) -> str:
        return self._state(subject_id)[0]

    def empty(self, subject_id: str) -> bool:
        value, blocks = self._state(subject_id)
        return not value and not blocks

    def body_chars(self, subject_id: str) -> int:
        """价值叙述 + 各场景块正文，不含 B 编号。与提示词「片场总长」同一口径。"""
        value, blocks = self._state(subject_id)
        return len(value) + sum(len(text) for text in blocks)

    def _block_offset(self, subject_id: str) -> int:
        """渲染时价值叙述占 B1，场景块从 B2 起 → 场景块下标偏移 1。"""
        return 1 if self.value_narration(subject_id) else 0

    def save(
        self,
        subject_id: str,
        blocks: Iterable[str],
    ) -> tuple[str, ...]:
        value = self.value_narration(subject_id)
        cleaned = tuple(
            str(text).strip() for text in blocks if str(text).strip()
        )
        self._zones[subject_id] = (value, cleaned)
        self._flush()
        return cleaned

    def boot(
        self,
        subject_id: str,
        value: str = "",
        scene: Iterable[str] = (),
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
        blocks = tuple(
            str(text).strip() for text in scene if str(text).strip()
        )
        self._zones[subject_id] = (narration, blocks)
        self._flush()
        return blocks

    def render(self, subject_id: str) -> str:
        value, blocks = self._state(subject_id)
        rows: list[str] = []
        n = 0
        if value:
            n += 1
            rows.append(f"B{n}  {value}")
        for text in blocks:
            n += 1
            rows.append(f"B{n}  {text}")
        return "\n".join(rows)

    def apply_edit(
        self,
        subject_id: str,
        edits: Sequence[Mapping[str, object]],
        *,
        append_text: str = "",
        append_blocks: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """按原始编号一次性应用模型给出的增/删/改，再追加程序块；不自动删开头。

        顺序：del/mod 已有块 → 模型 add → append_blocks → append_text。
        ``add`` 只应用 ``text``，忽略 ``id``。
        """
        blocks = list(self.get(subject_id))
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
            if scene_index < 0 or scene_index >= len(blocks):
                continue  # 引用价值叙述(B1)或不存在块 → 忽略
            if op == "del":
                del_indices.add(scene_index)
            elif op == "mod":
                text = _clean_block_text(edit.get("text"))
                if text:
                    mods[scene_index] = text
        result: list[str] = []
        for i, text in enumerate(blocks):
            if i in del_indices:
                continue
            result.append(mods.get(i, text))
        result.extend(add_blocks)
        extra = [*(append_blocks or ()), append_text]
        for text in extra:
            cleaned = str(text or "").strip()
            if cleaned:
                result.append(cleaned)
        return self.save(subject_id, result)

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {
                "value": value,
                "blocks": list(blocks),
            }
            for key, (value, blocks) in self._zones.items()
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
