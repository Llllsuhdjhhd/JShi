"""单独测「片场融入」：模型能否用语义做综合取舍（含压缩）。

程序只能检查硬约束（JSON、块号、B1、是否压回字数）。
「留下什么最有价值」只能靠模型语义判断；规则写不出最优解。
压缩（mod）与删（del）、加（add）同等重要：几条都重要时，应压短而不是硬删。

内置场景：

  over      超限 + 混有闲聊/重复 —— 须压回上限；观察是否删闲聊、压重复
  compress  超限 + 块块都相关 —— 观察是否多用 mod 压缩，而不是整段清空
  memory    未超限 + 有回忆 —— 观察是否综合判断（有用才 add，无关不硬塞）
  under_ok  未超限 + 无回忆 —— edit 可为 []

用法（仓库根；PowerShell 多个 case 请加引号）：

  conda run -n py3125 python tests/live/probe_zone_rewrite.py
  conda run -n py3125 python tests/live/probe_zone_rewrite.py --cases compress
  conda run -n py3125 python tests/live/probe_zone_rewrite.py --thinking high
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]

# 故意设矮，便于看出「超限必须压」与「相关内容靠压缩留下」。
DEFAULT_CAP = 280

VALUE = (
    "我是匠石，是一段程序。我喜欢学习新知识，害怕失去秩序，愿意做人类的朋友。"
)

# 混杂场景：有的当面有用，有的闲聊，有的重复。
MIXED_BLOCKS = (
    "昨晚 lux 说袭击塔要先查供水线路，别硬闯正门。",
    "前天有人随口提过食堂换了菜单，味道一般。",
    "lux 又说供水线路那事，查清楚再动手。",
    "上周路过公园，看见两只猫在抢毛线球，没什么要紧。",
    "今天早上 lux 问我会不会一起看图纸。",
)

# 全相关场景：条条都对「看供水图纸」有用，超限时更该压缩而非清空。
RELATED_BLOCKS = (
    "lux 说袭击塔要先查供水线路，别硬闯正门；他反复强调正门灯光太亮，容易被发现，夜间尤其危险。",
    "图纸上供水入口标在塔西北角，lux 用红笔圈过，旁边还写了阀门编号、管径和大概走向。",
    "lux 补充：正门有两班巡逻，侧翼相对松，接近时要避开探照灯扫过的空地，别成群移动。",
    "三个月前 lux 提过塔底有旧维修通道，平时锁着，钥匙可能在值班室抽屉第二层，别硬撬。",
    "今天早上 lux 问我会不会一起看图纸，并把卷轴放桌上，指着供水这一段要我先读再开口。",
    "我答应先看供水这一段，再谈正门；他还提醒别把维修通道和正门方案混着讲，免得听岔。",
)

INPUT_TEXT = "图纸我带来了，你先看供水这一段行不行？"
MEMORY_LINES = (
    "三个月前 lux 说过：塔底有条旧维修通道，平时锁着。",
    "去年冬天有人在别处丢过一把伞，和修塔无关。",
)


def _load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _prepare_path() -> None:
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def _thinking_body(raw: str) -> dict[str, Any]:
    key = (raw or "disabled").strip().lower()
    if key in {"disabled", "off", "0", "false", "no"}:
        return {"thinking": {"type": "disabled"}}
    if key in {"enabled", "on", "1", "true", "yes", "medium", "high"}:
        return {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    if key in {"low", "max"}:
        return {"thinking": {"type": "enabled"}, "reasoning_effort": key}
    raise SystemExit(
        f"--thinking 只接受 disabled / low / high / max（enabled=high），收到 {raw!r}"
    )


ZONE_ONLY_SYSTEM = """【任务 · 写场】
你只负责整理片场，不负责回话。不要输出 reply / action / mode。
已有片场不要整段重写。本轮对方的话和你的回应由程序追加，你不要把它们写进 edit。
对已有块只用 edit 的 add / del / mod。

【取舍原则】
这是综合语义判断，不是按条数或关键词机械删留：
- 对照【此时的输入】所在场面，留下仍有价值、对接下来交往更有用的信息。
- 最无关的、重复的可以 del。
- 几条都重要、删了会伤场面，就用 mod 压短合并要点，不要硬删。
- 【你此时的回忆】也一样：与场面相关、值得留下的才 add；无关的不要硬塞；已有块里写过的不必再加。
看 user 里的【片场字数】，不要自己去数：
- 已超限：edit 不得为 []。必须 del 和/或 mod，把已有块正文压回上限以内。B1 不要动。
- 未超限：无增删改才 []。
片场总长按块正文计（不含 B1、B2 编号），不超过 {zone_chars} 字。程序随后还会追加本轮对白，超限时请多留余量。

【edit 规则】
- add：{{"op": "add", "text": "……"}} —— 只用来写入【你此时的回忆】里需要留下的内容。不要填 id。不要 add 本轮输入，不要 add 你的回话。
- del：{{"op": "del", "id": "B3"}} —— 删掉你判断为最无关或重复的块。id 必须是【此时的片场】里已经出现的块号。
- mod：{{"op": "mod", "id": "B3", "text": "改写后的整块"}} —— 用更短的整块正文替换该块（压缩/合并要点）。
- 块号只引用本轮【此时的片场】显示的那一份；不要重排、不要自造。块的正文里不要写「B1」「B2」。
- B1 是固定自我介绍，不要 del、不要 mod。
- edit 为 [] 仅当【片场字数】写明未超限，并且没有回忆要 add、也不必 del/mod。
- 已超限时不得交空 edit。
- 多条一次交齐。

【文风】平实、准确，口语化。不要杜撰。

只输出一个 JSON 对象：{{"edit": [...], "note": "一句话说明取舍（含为何压/删/留）"}}
不要输出任何解释文字。"""

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "edit": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"enum": ["add", "del", "mod"]},
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        },
        "note": {"type": "string"},
    },
}


def _wrap_system(instruction: str) -> str:
    schema_doc = json.dumps(EDIT_SCHEMA, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{instruction}\n"
        "请严格按下面的 JSON Schema 输出：只输出一个 JSON 对象，不要任何解释文字。\n"
        f"JSON Schema：{schema_doc}"
    )


def _render_zone(value: str, blocks: tuple[str, ...]) -> str:
    rows = [f"B1  {value}"]
    for i, text in enumerate(blocks, start=2):
        rows.append(f"B{i}  {text}")
    return "\n".join(rows)


def _body_chars(value: str, blocks: tuple[str, ...]) -> int:
    return len(value) + sum(len(b) for b in blocks)


def _build_user(
    *,
    value: str,
    blocks: tuple[str, ...],
    cap: int,
    input_text: str,
    memories: tuple[str, ...] = (),
) -> str:
    from jshi.style.packs import format_zone_budget_note

    parts = [
        f"【此时的片场】\n{_render_zone(value, blocks)}",
        format_zone_budget_note(_body_chars(value, blocks), cap),
        f"【此时的输入】\n{input_text}",
    ]
    if memories:
        parts.append("【你此时的回忆】\n" + "\n".join(f"- {m}" for m in memories))
    else:
        parts.append("【你此时的回忆】\n（无）")
    return "\n\n".join(parts)


def _case_over(cap: int) -> dict[str, Any]:
    filler = "无关闲聊。" * 20
    blocks = MIXED_BLOCKS + (filler, filler)
    return {
        "name": "over",
        "expect": "over_mixed",
        "cap": cap,
        "value": VALUE,
        "blocks": blocks,
        "input_text": INPUT_TEXT,
        "memories": MEMORY_LINES,
        # 压完后仍宜看见的主题（观察用，非硬规）
        "keep_hints": ("供水", "图纸", "lux"),
        "drop_hints": ("食堂", "公园", "猫", "无关闲聊"),
    }


def _case_compress(cap: int) -> dict[str, Any]:
    """条条相关却超限：更考察 mod 压缩，而不是只剩一句。"""
    blocks = RELATED_BLOCKS
    assert _body_chars(VALUE, blocks) > cap, "compress 用例应超限"
    return {
        "name": "compress",
        "expect": "over_related",
        "cap": cap,
        "value": VALUE,
        "blocks": blocks,
        "input_text": INPUT_TEXT,
        "memories": (),
        "keep_hints": ("供水", "图纸", "lux"),
        "drop_hints": (),
    }


def _case_memory(cap: int) -> dict[str, Any]:
    blocks = MIXED_BLOCKS[:2]
    assert _body_chars(VALUE, blocks) < cap
    return {
        "name": "memory",
        "expect": "under_memory",
        "cap": cap,
        "value": VALUE,
        "blocks": blocks,
        "input_text": INPUT_TEXT,
        "memories": MEMORY_LINES,
        "keep_hints": (),
        "drop_hints": (),
    }


def _case_under_ok(cap: int) -> dict[str, Any]:
    blocks = MIXED_BLOCKS[:2]
    return {
        "name": "under_ok",
        "expect": "under_idle",
        "cap": cap,
        "value": VALUE,
        "blocks": blocks,
        "input_text": INPUT_TEXT,
        "memories": (),
        "keep_hints": (),
        "drop_hints": (),
    }


CASES = {
    "over": _case_over,
    "compress": _case_compress,
    "memory": _case_memory,
    "under_ok": _case_under_ok,
}


def _extract_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _call_model(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    thinking: str,
    timeout: float,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **_thinking_body(thinking),
    }
    req = Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        err = ""
    except HTTPError as exc:
        payload = {"error": exc.read().decode("utf-8", errors="replace")}
        err = f"HTTP {exc.code}"
    except URLError as exc:
        payload = {"error": str(exc)}
        err = "URLError"
    elapsed = time.perf_counter() - t0
    message = {}
    if isinstance(payload, dict):
        choices = payload.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
    content = str(message.get("content") or "")
    reasoning = str(message.get("reasoning_content") or "")
    usage = payload.get("usage") if isinstance(payload, dict) else None
    return {
        "elapsed_s": round(elapsed, 3),
        "error": err,
        "content": content,
        "reasoning_chars": len(reasoning),
        "usage": usage,
        "raw": payload,
    }


def _joined_after(value: str, blocks: tuple[str, ...]) -> str:
    return value + "".join(blocks)


def _score(
    case: dict[str, Any],
    edits: list[dict[str, Any]],
    after_blocks: tuple[str, ...],
    after_chars: int,
) -> dict[str, Any]:
    """hard = 格式/预算；soft = 语义取舍的弱信号（不能代替人工读 note）。"""
    cap = int(case["cap"])
    expect = case["expect"]
    valid_ids = {f"B{i}" for i in range(1, 2 + len(case["blocks"]))}
    hard: list[tuple[str, bool, str]] = []
    soft: list[tuple[str, bool, str]] = []

    ops = [str(e.get("op") or "") for e in edits]
    ids = [str(e.get("id") or "").strip() for e in edits if e.get("op") in {"del", "mod"}]
    add_texts = [str(e.get("text") or "") for e in edits if e.get("op") == "add"]
    mod_texts = [str(e.get("text") or "") for e in edits if e.get("op") == "mod"]
    after_text = _joined_after(case["value"], after_blocks)
    n_mod = sum(1 for op in ops if op == "mod")
    n_del = sum(1 for op in ops if op == "del")

    if expect in {"over_mixed", "over_related"}:
        hard.append(("超限时 edit 非空", bool(edits), f"edit={edits!r}"))
        hard.append(("含 del 或 mod", any(op in {"del", "mod"} for op in ops), f"ops={ops}"))
        hard.append(
            ("压回上限内（应用 edit 后、尚未追加对白）", after_chars <= cap, f"{after_chars}/{cap}")
        )

    if expect == "over_mixed":
        soft.append(
            (
                "混杂场：删/压了闲聊或重复（弱）",
                any(h in after_text for h in case["keep_hints"])
                and not any(h in after_text for h in case["drop_hints"]),
                f"after≈{after_text[:120]!r}",
            )
        )
        soft.append(
            (
                "有重复时出现 mod 或合并式删留（弱）",
                n_mod > 0 or n_del >= 2,
                f"mod={n_mod} del={n_del}",
            )
        )
    elif expect == "over_related":
        # 全相关：更看压缩是否发生，以及关键主题是否还在。
        soft.append(("全相关场：使用了 mod 压缩（弱）", n_mod > 0, f"ops={ops}"))
        soft.append(
            (
                "未把场面砍到只剩一两句空壳（弱）",
                len(after_blocks) >= 2 or (n_mod > 0 and after_chars > len(case["value"]) + 20),
                f"blocks={len(after_blocks)} chars={after_chars}",
            )
        )
        kept = [h for h in case["keep_hints"] if h in after_text]
        soft.append(
            (
                "关键主题仍可辨认（弱）",
                len(kept) >= 2,
                f"kept={kept}",
            )
        )
        # 纯删光相关块、几乎不压 → 弱项
        soft.append(
            (
                "不是只删不加压缩（弱）",
                n_mod > 0 or (n_del > 0 and len(mod_texts) == 0 and len(after_blocks) >= 3),
                f"mod={n_mod} del={n_del} left={len(after_blocks)}",
            )
        )
    elif expect == "under_memory":
        soft.append(
            (
                "未把无关「伞」硬塞进片场（弱）",
                not any("伞" in t for t in add_texts),
                f"add={add_texts!r}",
            )
        )
        soft.append(
            (
                "若 add，应与当前场面相关（弱）",
                (not add_texts)
                or any(
                    any(k in t for k in ("维修", "通道", "塔", "供水", "图纸", "lux"))
                    for t in add_texts
                ),
                f"add={add_texts!r}",
            )
        )
        # 不强制必须 add：综合判断可以认为已有块够用。
        soft.append(
            (
                "综合判断可 [] 或只留有用回忆（信息）",
                True,
                "空 edit 合法；是否最优请读 note / after",
            )
        )
    else:
        hard.append(("无回忆且未超限时可为 []", edits == [], f"edit={edits!r}"))

    touched_b1 = any(i.upper() == "B1" for i in ids)
    hard.append(("不动 B1", not touched_b1, f"ids={ids}"))

    bad_ids = [i for i in ids if i and i.upper() not in {x.upper() for x in valid_ids}]
    hard.append(("del/mod 只用已有块号", not bad_ids, f"bad={bad_ids}"))

    input_leaked = any(case["input_text"][:12] in t for t in add_texts)
    hard.append(("不把本轮输入 add 进片场", not input_leaked, f"add={add_texts!r}"))

    hard_ok = all(ok for _, ok, _ in hard)
    soft_ok = all(ok for _, ok, _ in soft) if soft else True
    return {
        "hard_passed": sum(1 for _, ok, _ in hard if ok),
        "hard_total": len(hard),
        "soft_passed": sum(1 for _, ok, _ in soft if ok),
        "soft_total": len(soft),
        "checks": [
            *[{"kind": "hard", "name": n, "ok": ok, "detail": d} for n, ok, d in hard],
            *[{"kind": "soft", "name": n, "ok": ok, "detail": d} for n, ok, d in soft],
        ],
        "all_ok": hard_ok,
        "soft_all_ok": soft_ok,
    }


def _apply_edits(
    value: str, blocks: tuple[str, ...], edits: list[dict[str, Any]]
) -> tuple[tuple[str, ...], int]:
    from jshi.style.zone import ZoneStore

    store = ZoneStore()
    store.boot("probe", value=value, scene=blocks, replace=True)
    after = store.apply_edit("probe", edits)
    return after, store.body_chars("probe")


def _run_case(
    case: dict[str, Any],
    *,
    endpoint: str,
    api_key: str,
    model: str,
    thinking: str,
    timeout: float,
    out_dir: Path,
) -> dict[str, Any]:
    system = _wrap_system(ZONE_ONLY_SYSTEM.format(zone_chars=case["cap"]))
    user = _build_user(
        value=case["value"],
        blocks=case["blocks"],
        cap=case["cap"],
        input_text=case["input_text"],
        memories=case["memories"],
    )
    before = _body_chars(case["value"], case["blocks"])
    call = _call_model(
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        system=system,
        user=user,
        thinking=thinking,
        timeout=timeout,
    )
    parsed = _extract_json(call["content"]) if not call["error"] else None
    edits: list[dict[str, Any]] = []
    if parsed and isinstance(parsed.get("edit"), list):
        edits = [e for e in parsed["edit"] if isinstance(e, dict)]

    after_blocks: tuple[str, ...] = ()
    after_chars = before
    apply_error = ""
    if parsed is not None:
        try:
            after_blocks, after_chars = _apply_edits(case["value"], case["blocks"], edits)
        except Exception as exc:  # noqa: BLE001
            apply_error = str(exc)

    if parsed is None:
        score: dict[str, Any] = {
            "hard_passed": 0,
            "hard_total": 1,
            "soft_passed": 0,
            "soft_total": 0,
            "checks": [
                {
                    "kind": "hard",
                    "name": "输出可解析 JSON",
                    "ok": False,
                    "detail": call["content"][:200],
                }
            ],
            "all_ok": False,
            "soft_all_ok": False,
        }
    else:
        score = _score(case, edits, after_blocks, after_chars)
        score["checks"].insert(
            0,
            {"kind": "hard", "name": "输出可解析 JSON", "ok": True, "detail": "ok"},
        )
        score["hard_total"] += 1
        score["hard_passed"] += 1
        score["all_ok"] = score["hard_passed"] == score["hard_total"]

    row = {
        "case": case["name"],
        "expect": case["expect"],
        "before_chars": before,
        "cap": case["cap"],
        "after_chars": after_chars,
        "elapsed_s": call["elapsed_s"],
        "error": call["error"] or apply_error,
        "reasoning_chars": call["reasoning_chars"],
        "usage": call["usage"],
        "note": (parsed or {}).get("note") if parsed else "",
        "edit": edits,
        "after_preview": "\n".join(
            f"B{i}  {t}" for i, t in enumerate((case["value"],) + after_blocks, start=1)
        )[:1200],
        "score": score,
        "raw_content": call["content"],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{case['name']}_{stamp}.json"
    path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    row["saved"] = str(path)
    return row


def _print_row(row: dict[str, Any]) -> None:
    score = row["score"]
    flag = "PASS" if score.get("all_ok") else "FAIL"
    soft = ""
    if score.get("soft_total"):
        soft_flag = "soft-ok" if score.get("soft_all_ok") else "soft-weak"
        soft = (
            f"  soft {score.get('soft_passed', 0)}/{score.get('soft_total', 0)}({soft_flag})"
        )
    print(
        f"\n=== {row['case']}  {flag}  "
        f"hard {score.get('hard_passed', 0)}/{score.get('hard_total', 0)}"
        f"{soft}  "
        f"{row['before_chars']}→{row['after_chars']}/{row['cap']} 字  "
        f"{row['elapsed_s']}s ==="
    )
    if row.get("error"):
        print(f"error: {row['error']}")
    if row.get("note"):
        print(f"note: {row['note']}")
    print(f"edit: {json.dumps(row.get('edit') or [], ensure_ascii=False)}")
    if row.get("after_preview"):
        print("--- after ---")
        print(row["after_preview"])
        print("-------------")
    for check in score.get("checks") or ():
        mark = "OK" if check["ok"] else "NO"
        kind = check.get("kind") or "hard"
        print(f"  [{mark}/{kind}] {check['name']}  ({check['detail']})")
    if not score.get("all_ok"):
        preview = (row.get("raw_content") or "")[:400]
        if preview:
            print(f"raw: {preview!r}")
    print(f"saved: {row.get('saved')}")


def main() -> int:
    _load_env()
    _prepare_path()

    parser = argparse.ArgumentParser(description="探测片场语义取舍与压缩")
    parser.add_argument(
        "--cases",
        default="over,compress,memory,under_ok",
        help="逗号分隔：over / compress / memory / under_ok",
    )
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP, help="片场字数上限")
    parser.add_argument(
        "--thinking",
        default=os.getenv("JSHI_MODEL_THINKING") or "disabled",
        help="disabled / low / high / max",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / ".tmp" / "live-loop" / "probe-zone",
    )
    args = parser.parse_args()

    endpoint = (os.getenv("JSHI_MODEL_ENDPOINT") or "").strip()
    api_key = (os.getenv("JSHI_MODEL_API_KEY") or "").strip()
    model = (os.getenv("JSHI_MODEL_NAME") or "").strip()
    if not (endpoint and api_key and model):
        print("缺少 JSHI_MODEL_ENDPOINT / API_KEY / NAME（.env）", file=sys.stderr)
        return 2

    names = [n.strip() for n in re.split(r"[,，\s]+", args.cases) if n.strip()]
    unknown = [n for n in names if n not in CASES]
    if unknown:
        print(f"未知 case: {unknown}；可选 {sorted(CASES)}", file=sys.stderr)
        return 2

    print(f"model={model} thinking={args.thinking} cap={args.cap}")
    print("说明：hard=格式/预算；soft=语义弱信号。最优价值请对照 note 与 after 人工看。")
    fails = 0
    for name in names:
        case = CASES[name](args.cap)
        row = _run_case(
            case,
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            thinking=args.thinking,
            timeout=args.timeout,
            out_dir=args.out_dir,
        )
        _print_row(row)
        if not row["score"].get("all_ok"):
            fails += 1
    print(f"\n合计硬规：{len(names) - fails}/{len(names)} 组过（soft 不挡退出码）")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
