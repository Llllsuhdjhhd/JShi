"""205 引擎命令检索：Hermes 式分层披露 + BM25。

不执行工具。search / describe 只给策划看名单与说明。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

DIRECT_MAX_TOOLS = 8
LISTING_MAX_CHARS = 4000
LISTING_DESC_CHARS = 80
SEARCH_DEFAULT_LIMIT = 5
SEARCH_MAX_LIMIT = 25
SEARCH_MAX_QUERIES = 5
SEARCH_MERGED_LIMIT = 10
DESCRIBE_MAX_NAMES = 8
DESCRIBE_MAX_CHARS = 2000
PARAMS_MAX_CHARS = 2000
LOOKUP_MAX_ROUNDS = 6
BM25_K1 = 1.2
BM25_B = 0.75
NAME_EXACT_BOOST = 50.0
NAME_SUBSTRING_BOOST = 10.0

_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_SPLIT_NAME = re.compile(r"[_\-/.\s]+")

TIER_EAGER = "eager"
TIER_LISTING = "listing"
TIER_NAMES = "names"
TIER_INDEX = "index"


@dataclass(frozen=True)
class LookupAsk:
    """模型在策划完成前发出的检索请求。不漏出 SkillPlanner.plan。"""

    op: str
    queries: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    limit: int | None = None


@dataclass(frozen=True)
class ToolEntry:
    name: str
    description: str
    source: str
    params: Mapping[str, Any]
    tokens: tuple[str, ...]
    path: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Disclosure:
    tier: str
    total: int
    listing: tuple[Mapping[str, Any], ...]
    sources: tuple[Mapping[str, Any], ...]
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "total": self.total,
            "listing": [dict(item) for item in self.listing],
            "sources": [dict(item) for item in self.sources],
            "note": self.note,
        }


def tokenize(text: str) -> tuple[str, ...]:
    """英文词干 + 下划线切分 + 汉字双字。"""
    raw = (text or "").strip().lower()
    if not raw:
        return ()
    out: list[str] = []
    for piece in _SPLIT_NAME.split(raw):
        if not piece:
            continue
        for match in _ASCII_WORD.finditer(piece):
            stemmed = _stem(match.group())
            if stemmed:
                out.append(stemmed)
        for run in _CJK_RUN.findall(piece):
            if len(run) == 1:
                out.append(run)
            else:
                out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tuple(out)


def _stem(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]
    if word.endswith("ed") and len(word) > 4:
        return word[:-2]
    if word.endswith("es") and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _clip(text: str, limit: int) -> str:
    value = (text or "").strip()
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _param_blob(params: Any) -> str:
    if not isinstance(params, Mapping):
        return ""
    parts: list[str] = []
    props = params.get("properties")
    if isinstance(props, Mapping):
        for key, spec in props.items():
            parts.append(str(key))
            if isinstance(spec, Mapping):
                parts.append(str(spec.get("description") or ""))
        return " ".join(parts)
    for key, spec in params.items():
        if key in {"type", "$schema"}:
            continue
        parts.append(str(key))
        if isinstance(spec, Mapping):
            parts.append(str(spec.get("description") or spec.get("title") or ""))
        else:
            parts.append(str(spec))
    return " ".join(parts)


def _param_keys(params: Any) -> list[str]:
    if not isinstance(params, Mapping):
        return []
    props = params.get("properties")
    if isinstance(props, Mapping):
        return [str(key) for key in props]
    return [str(key) for key in params if key not in {"type", "$schema"}]


def parse_lookup(data: Mapping[str, Any]) -> LookupAsk | None:
    raw = data.get("lookup")
    if not isinstance(raw, Mapping):
        return None
    op = str(raw.get("op") or "").strip().lower()
    if op not in {"search", "describe"}:
        return None
    queries: list[str] = []
    single = str(raw.get("query") or "").strip()
    if single:
        queries.append(single)
    extra = raw.get("queries")
    if isinstance(extra, Sequence) and not isinstance(extra, (str, bytes)):
        for item in extra:
            text = str(item or "").strip()
            if text:
                queries.append(text)
    names: list[str] = []
    one = str(raw.get("name") or "").strip()
    if one:
        names.append(one)
    many = raw.get("names")
    if isinstance(many, Sequence) and not isinstance(many, (str, bytes)):
        for item in many:
            text = str(item or "").strip()
            if text:
                names.append(text)
    limit_raw = raw.get("limit")
    limit: int | None
    try:
        limit = int(limit_raw) if limit_raw is not None and str(limit_raw).strip() != "" else None
    except (TypeError, ValueError):
        limit = None
    return LookupAsk(
        op=op,
        queries=tuple(queries[:SEARCH_MAX_QUERIES]),
        names=tuple(names),
        limit=limit,
    )


def _merge_hits(
    groups: Sequence[Mapping[str, Any]], limit: int = SEARCH_MERGED_LIMIT
) -> list[dict[str, Any]]:
    """把多 query 的命中按 name 合并，取每个 name 的最高分，输出 top-K。"""
    best: dict[str, dict[str, Any]] = {}
    for group in groups:
        for hit in group.get("hits") or ():
            if not isinstance(hit, Mapping):
                continue
            name = str(hit.get("name") or "").strip()
            if not name:
                continue
            score = hit.get("score")
            if not isinstance(score, (int, float)):
                score = 0.0
            score = float(score)
            current = best.get(name)
            if current is None or score > float(current.get("score") or 0.0):
                best[name] = dict(hit)
                best[name]["score"] = round(score, 4)
    merged = sorted(
        best.values(),
        key=lambda item: (-float(item.get("score") or 0.0), str(item.get("name") or "")),
    )
    return merged[: max(0, limit)]


class ToolIndex:
    """一次策划用的引擎目录索引。"""

    def __init__(self, tools: Sequence[Mapping[str, Any]] = ()) -> None:
        self._entries: tuple[ToolEntry, ...] = tuple(
            _entry_from_map(item) for item in tools if str(item.get("name") or "").strip()
        )
        self._by_name = {item.name: item for item in self._entries}
        self._df: dict[str, int] = defaultdict(int)
        lengths: list[int] = []
        for entry in self._entries:
            lengths.append(len(entry.tokens))
            for token in set(entry.tokens):
                self._df[token] += 1
        self._avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0
        self._n = len(self._entries)
        self._disclosure = self._build_disclosure()

    @classmethod
    def from_maps(cls, tools: Sequence[Mapping[str, Any]] = ()) -> "ToolIndex":
        return cls(tools)

    @property
    def total(self) -> int:
        return self._n

    @property
    def names(self) -> set[str]:
        return set(self._by_name)

    def observed(self, name: str) -> Mapping[str, Any]:
        """返回某命令的实测统计（若有）。"""
        entry = self._by_name.get(name)
        if entry is None:
            return {}
        meta = entry.meta or {}
        observed = meta.get("observed")
        return observed if isinstance(observed, Mapping) else {}

    @property
    def tier(self) -> str:
        return self._disclosure.tier

    @property
    def is_eager(self) -> bool:
        return self._disclosure.tier == TIER_EAGER

    def disclose(self) -> Disclosure:
        return self._disclosure

    def eager_tools(self) -> list[dict[str, Any]]:
        return [self._compact(entry, full=True) for entry in self._entries]

    def search(
        self,
        query: str,
        *,
        limit: int = SEARCH_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        cap = max(1, min(int(limit or SEARCH_DEFAULT_LIMIT), SEARCH_MAX_LIMIT))
        q_tokens = tokenize(query)
        if not q_tokens:
            return {
                "hits": [],
                "hint": "查询是空的。用功能词检索，例如 weather、机票。",
                "rarest": "",
            }
        in_corpus = [token for token in q_tokens if self._df.get(token, 0) > 0]
        if not in_corpus:
            return {
                "hits": [],
                "hint": "这些词目录里都没有。换功能词检索，不要带地点、人名。",
                "rarest": "",
            }
        rarest = min(in_corpus, key=lambda token: (self._df[token], token))
        scored: list[tuple[float, ToolEntry]] = []
        for entry in self._entries:
            bag = set(entry.tokens)
            if rarest not in bag:
                continue
            score = self._bm25(q_tokens, entry)
            lowered = entry.name.lower()
            needle = (query or "").strip().lower()
            if needle and needle == lowered:
                score += NAME_EXACT_BOOST
            elif needle and needle in lowered:
                score += NAME_SUBSTRING_BOOST
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        hits = [
            {
                "name": entry.name,
                "description": _clip(entry.description, LISTING_DESC_CHARS),
                "source": entry.source,
                "score": round(score, 4),
            }
            for score, entry in scored[:cap]
        ]
        if not hits:
            return {
                "hits": [],
                "hint": "最稀有的功能词没有落到任何命令上。换更具体的工具名或功能词再 search。",
                "rarest": rarest,
            }
        return {"hits": hits, "hint": "", "rarest": rarest}

    def describe(self, names: Sequence[str]) -> dict[str, Any]:
        seen: list[str] = []
        for name in names:
            text = str(name or "").strip()
            if text and text not in seen:
                seen.append(text)
            if len(seen) >= DESCRIBE_MAX_NAMES:
                break
        found: list[dict[str, Any]] = []
        missing: list[str] = []
        for name in seen:
            entry = self._by_name.get(name)
            if entry is None:
                missing.append(name)
                continue
            found.append(self._compact(entry, full=True))
        return {"tools": found, "not_found": missing}

    def dispatch(self, ask: LookupAsk) -> dict[str, Any]:
        if ask.op == "describe":
            return {"op": "describe", **self.describe(ask.names)}
        queries = (ask.queries or ask.names)[:SEARCH_MAX_QUERIES]
        if not queries:
            return {
                "op": "search",
                "hits": [],
                "hint": "search 需要 query。用功能词，不要整句 need。",
                "rarest": "",
                "queries": [],
            }
        limit = ask.limit if ask.limit is not None else SEARCH_DEFAULT_LIMIT
        groups = []
        for query in queries:
            groups.append({"query": query, **self.search(query, limit=limit)})
        merged = _merge_hits(groups)
        if len(groups) == 1:
            return {
                "op": "search",
                "queries": list(queries),
                **groups[0],
                "merged": merged,
            }
        return {
            "op": "search",
            "queries": list(queries),
            "groups": groups,
            "merged": merged,
        }

    def format_view(self, *, raw: bool = False, limit: int = 40) -> list[str]:
        if self._n == 0:
            return ["（无）"]
        cap = self._n if raw else min(self._n, max(1, limit))
        lines: list[str] = []
        if not self.is_eager:
            lines.append(
                f"tier={self.tier}  total={self.total}  完整说明用 search/describe"
            )
        shown = self._disclosure.listing[:cap] if self._disclosure.listing else []
        if not shown and self.is_eager:
            shown = [
                {"name": item.name, "description": item.description}
                for item in self._entries[:cap]
            ]
        for item in shown:
            name = str(item.get("name") or "").strip() or "—"
            note = str(item.get("description") or "").strip()
            if note:
                width = 400 if raw else 120
                lines.append(f"- {name}  {_clip(note, width)}")
            else:
                lines.append(f"- {name}")
        leftover = self._n - len(shown)
        if leftover > 0:
            lines.append(f"（另有 {leftover} 条未列出）")
        return lines or ["（无）"]

    def _build_disclosure(self) -> Disclosure:
        sources = tuple(
            {"source": key, "count": count}
            for key, count in sorted(Counter(item.source for item in self._entries).items())
        )
        note_search = "完整参数用 lookup.describe；按意图用 lookup.search。lookup 不是执行。"
        if self._n <= DIRECT_MAX_TOOLS:
            return Disclosure(
                tier=TIER_EAGER,
                total=self._n,
                listing=(),
                sources=sources,
                note="目录已齐，直接从 engine_tools 选 command。",
            )
        listing_full = tuple(
            {"name": item.name, "description": _clip(item.description, LISTING_DESC_CHARS)}
            for item in self._entries
        )
        if _json_size(listing_full) <= LISTING_MAX_CHARS:
            return Disclosure(
                tier=TIER_LISTING,
                total=self._n,
                listing=listing_full,
                sources=sources,
                note=note_search,
            )
        names_only = tuple({"name": item.name} for item in self._entries)
        if _json_size(names_only) <= LISTING_MAX_CHARS:
            return Disclosure(
                tier=TIER_NAMES,
                total=self._n,
                listing=names_only,
                sources=sources,
                note=note_search,
            )
        return Disclosure(
            tier=TIER_INDEX,
            total=self._n,
            listing=(),
            sources=sources,
            note="名单放不下。必须 lookup.search，再 describe 后点名。",
        )

    def _bm25(self, query_tokens: Sequence[str], entry: ToolEntry) -> float:
        if self._n == 0:
            return 0.0
        tf = Counter(entry.tokens)
        dl = len(entry.tokens) or 1
        score = 0.0
        for token in query_tokens:
            df = self._df.get(token, 0)
            if df <= 0:
                continue
            freq = tf.get(token, 0)
            if freq <= 0:
                continue
            idf = math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))
            denom = freq + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / self._avgdl)
            score += idf * (freq * (BM25_K1 + 1.0)) / denom
        return score

    def _compact(self, entry: ToolEntry, *, full: bool) -> dict[str, Any]:
        desc = (
            _clip(entry.description, DESCRIBE_MAX_CHARS)
            if full
            else _clip(entry.description, LISTING_DESC_CHARS)
        )
        out: dict[str, Any] = {
            "name": entry.name,
            "description": desc,
            "source": entry.source,
        }
        if entry.path:
            out["path"] = entry.path
        if entry.meta:
            out["meta"] = dict(entry.meta)
        if full and entry.params:
            dumped = json.dumps(entry.params, ensure_ascii=False)
            if len(dumped) > PARAMS_MAX_CHARS:
                out["params"] = {
                    "_clipped": True,
                    "keys": _param_keys(entry.params)[:40],
                }
            else:
                out["params"] = dict(entry.params)
        return out


def _entry_from_map(item: Mapping[str, Any]) -> ToolEntry:
    name = str(item.get("name") or "").strip()
    description = str(item.get("description") or item.get("detail") or "").strip()
    source = str(item.get("source") or "engine").strip() or "engine"
    path = str(item.get("path") or "").strip()
    params = item.get("params")
    if not isinstance(params, Mapping):
        params = item.get("parameters")
    if not isinstance(params, Mapping):
        params = item.get("schema")
    if not isinstance(params, Mapping):
        params = {}
    meta = item.get("meta")
    if not isinstance(meta, Mapping):
        meta = {}
    blob = " ".join((name, description, _param_blob(params)))
    return ToolEntry(
        name=name,
        description=description,
        source=source,
        params=dict(params),
        tokens=tokenize(blob),
        path=path,
        meta=dict(meta),
    )


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
