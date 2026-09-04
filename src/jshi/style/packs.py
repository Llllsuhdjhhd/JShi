"""风格包：可注册；每包两套提示词槽位（首次 / 续写）。正文另议，这里不写死。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

WOOD = "wood"
SUXIPO = "suxipo"
SMITH = "smith"
DEFAULT_PACK = WOOD


@dataclass(frozen=True)
class StylePack:
    """一份写法。新增人格：构造并 ``registry.register``，不必改主链路。

    ``instruction_first`` / ``instruction_continue`` 是气口，正文稍后填。
    """

    pack_id: str
    display_name: str = ""
    aliases: tuple[str, ...] = ()
    instruction_first: str = ""
    instruction_continue: str = ""

    def instruction(self, *, first: bool) -> str:
        return self.instruction_first if first else self.instruction_continue


class StylePackRegistry:
    """风格包登记。主链路只问 registry，不写死包名。"""

    def __init__(self, packs: Iterable[StylePack] = ()) -> None:
        self._packs: dict[str, StylePack] = {}
        self._aliases: dict[str, str] = {}
        for pack in packs:
            self.register(pack)

    def register(self, pack: StylePack) -> None:
        key = (pack.pack_id or "").strip()
        if not key:
            return
        self._packs[key] = pack
        self._aliases[key] = key
        self._aliases[key.casefold()] = key
        if pack.display_name:
            self._aliases[pack.display_name] = key
        for alias in pack.aliases:
            text = (alias or "").strip()
            if text:
                self._aliases[text] = key
                self._aliases[text.casefold()] = key

    def get(self, pack_id: str) -> StylePack | None:
        return self._packs.get(pack_id)

    def resolve(self, raw: str | None) -> StylePack:
        text = (raw or "").strip()
        if not text:
            return self._packs[DEFAULT_PACK]
        key = self._aliases.get(text) or self._aliases.get(text.casefold())
        if key and key in self._packs:
            return self._packs[key]
        if text in self._packs:
            return self._packs[text]
        return self._packs[DEFAULT_PACK]

    def all_packs(self) -> tuple[StylePack, ...]:
        return tuple(self._packs.values())

    @property
    def pack_ids(self) -> frozenset[str]:
        return frozenset(self._packs)


def is_first_style_turn(
    context_text: str,
    zone_pack_id: str,
    selected_pack_id: str,
) -> bool:
    """程序判断首次：空现场，或现场还不是当前风格写的（含刚切换）。"""
    if not (context_text or "").strip():
        return True
    previous = (zone_pack_id or "").strip()
    return not previous or previous != selected_pack_id


def builtin_packs() -> tuple[StylePack, ...]:
    """内置三份只占名与气口，写法正文不在这里定。"""
    return (
        StylePack(pack_id=WOOD, display_name="木头", aliases=("木头", "wood")),
        StylePack(pack_id=SUXIPO, display_name="苏西坡", aliases=("苏西坡", "suxipo")),
        StylePack(pack_id=SMITH, display_name="斯密斯", aliases=("斯密斯", "smith")),
    )


DEFAULT_REGISTRY = StylePackRegistry(builtin_packs())

PACK_IDS = DEFAULT_REGISTRY.pack_ids
DISPLAY_NAMES = {
    pack.pack_id: pack.display_name for pack in DEFAULT_REGISTRY.all_packs()
}


def normalize_pack_id(
    raw: str | None,
    registry: StylePackRegistry | None = None,
) -> str:
    return (registry or DEFAULT_REGISTRY).resolve(raw).pack_id


def instruction_for(
    pack_id: str | None,
    *,
    first: bool = False,
    registry: StylePackRegistry | None = None,
) -> str:
    pack = (registry or DEFAULT_REGISTRY).resolve(pack_id)
    return pack.instruction(first=first)


class StylePackStore:
    """主体当前风格包。落盘后重启仍有效，直到主动切换。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        registry: StylePackRegistry | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.registry = registry or DEFAULT_REGISTRY
        self._packs: dict[str, str] = {}
        if self.path is not None and self.path.is_file():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._packs = {
                    str(key): self.registry.resolve(str(value)).pack_id
                    for key, value in data.items()
                }

    def get(self, subject_id: str) -> str:
        return self._packs.get(subject_id, DEFAULT_PACK)

    def set(self, subject_id: str, pack_id: str) -> str:
        chosen = self.registry.resolve(pack_id).pack_id
        self._packs[subject_id] = chosen
        self._flush()
        return chosen

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._packs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
