"""单独打 DeepSeek Chat Completions，不走匠石认知主链路。

对比同一份提示词在不同请求下的墙钟时间和用量：
  omit      不传 thinking（旧适配器；V4 Flash 此时默认开思考）
  disabled  显式关闭思考  thinking.type=disabled
  enabled   开思考 + reasoning_effort=high（等同 high）
  low/high/max  开思考并设对应 reasoning_effort

默认用 .jshi 里现成的斯密斯提示词 + 片场拼一份接近 /prompt 的材料；
加 --from-jshi 则走 TalkSession._prompt_parts，与对话里 /prompt 同一份。

用法（仓库根；HTTP 本身不依赖 torch，--from-jshi 组装提示词才需要 py3125）：

  conda run -n py3125 python tests/live/probe_deepseek_thinking.py
  conda run -n py3125 python tests/live/probe_deepseek_thinking.py --from-jshi
  conda run -n py3125 python tests/live/probe_deepseek_thinking.py --prompt-file .pytest/live-loop/probe-thinking/prompt.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]


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
    neighbor = ROOT.parent / "Jshi_memory" / "src"
    if (neighbor / "rems").is_dir():
        path = str(neighbor)
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)


def _wrap_system(instruction: str, schema: Any) -> str:
    schema_doc = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{instruction}\n"
        "请严格按下面的 JSON Schema 输出：只输出一个 JSON 对象，不要任何解释文字，"
        "枚举字段必须取枚举值。\n"
        f"JSON Schema：{schema_doc}"
    )


def _iter_traces(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "recall_traces.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _last_query(data_dir: Path, fallback: str) -> str:
    last = ""
    for row in _iter_traces(data_dir):
        query = str(row.get("query") or "").strip()
        if query and query != "你好":
            last = query
    return last or fallback


def _memory_lines(data_dir: Path, query: str) -> list[str]:
    """只读 rems.db，不碰 Qdrant，避免和正在跑的 talk 抢锁。"""
    traces = _iter_traces(data_dir)
    chosen: dict[str, Any] | None = None
    for row in traces:
        if str(row.get("query") or "").strip() == query:
            chosen = row
    if chosen is None:
        for row in reversed(traces):
            if str(row.get("query") or "").strip() and str(row.get("query")).strip() != "你好":
                chosen = row
                break
    ids = [str(item) for item in (chosen or {}).get("recalled_event_ids") or () if str(item)]
    db = data_dir / "rems" / "rems.db"
    if not ids or not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    placeholders = ",".join("?" * len(ids))
    found = {
        str(event_id): (text or "").strip()
        for event_id, text in con.execute(
            f"SELECT event_id, content_raw FROM events WHERE event_id IN ({placeholders})",
            ids,
        )
    }
    lines: list[str] = []
    for event_id in ids:
        text = found.get(event_id, "")
        if text:
            lines.append(f"memory:{event_id}：{text}")
    return lines


def _reconstruct_prompt(data_dir: Path, query: str) -> tuple[str, str]:
    from jshi.core.params import suxipo_zone_chars
    from jshi.style.packs import SMITH, instruction_for, schema_for
    from jshi.style.zone import ZoneStore

    instruction = instruction_for(SMITH).replace("{zone_chars}", str(suxipo_zone_chars()))
    schema = schema_for(SMITH)
    if schema is None:
        raise SystemExit("斯密斯人格没有 schema，无法拼提示词。")
    scene = ZoneStore(data_dir / "zone.json").render("stone")
    memories = _memory_lines(data_dir, query)
    parts = [
        f"【此时的片场】\n{scene or '（片场为空）'}",
        f"【此时的输入】\nlux：{query}",
    ]
    if memories:
        parts.append("【你此时的回忆】\n" + "\n".join(memories))
    return _wrap_system(instruction, schema), "\n\n".join(parts)


def _dump_from_jshi(data_dir: Path, query: str) -> tuple[str, str]:
    os.chdir(ROOT)
    os.environ["JSHI_DATA_DIR"] = str(data_dir)
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    from jshi.app.cli import _runtime
    from jshi.app.talk_session import prepare_talk

    process, identities, _subjects = _runtime(data_dir)
    session = prepare_talk(
        process,
        identities,
        data_dir,
        subject_id=None,
        speaker=None,
        channel=None,
        carriers=(),
    )
    session.last_line = query
    result = session._prompt_parts()
    if isinstance(result, str):
        raise SystemExit(result)
    return result


def _load_prompt_file(path: Path) -> tuple[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    system = str(data.get("system") or "")
    user = str(data.get("user") or "")
    if not system or not user:
        raise SystemExit(f"提示词文件缺 system/user：{path}")
    return system, user


def _post(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    extra: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    payload.update(extra)
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", None) or response.getcode()
        elapsed = time.perf_counter() - started
        result = json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        elapsed = time.perf_counter() - started
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        return {
            "ok": False,
            "elapsed_s": round(elapsed, 3),
            "error": f"HTTP {exc.code}",
            "detail": detail,
        }
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        elapsed = time.perf_counter() - started
        return {
            "ok": False,
            "elapsed_s": round(elapsed, 3),
            "error": type(exc).__name__,
            "detail": str(exc)[:800],
        }
    message = ((result.get("choices") or [{}])[0].get("message")) or {}
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content") or ""
    usage = result.get("usage") or {}
    return {
        "ok": True,
        "elapsed_s": round(elapsed, 3),
        "http_status": status,
        "response_model": result.get("model"),
        "content_chars": len(content),
        "reasoning_chars": len(reasoning),
        "content_preview": content[:240],
        "reasoning_preview": reasoning[:240],
        "usage": usage,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "completion_details": usage.get("completion_tokens_details"),
    }


VARIANTS: dict[str, dict[str, Any]] = {
    "omit": {},
    "disabled": {"thinking": {"type": "disabled"}},
    "enabled": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
    "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
    "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
    "max": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
}


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 DeepSeek thinking 对总耗时的影响")
    parser.add_argument(
        "--variants",
        default="disabled,omit",
        help="逗号分隔：omit / disabled / enabled / low / high / max，默认 disabled,omit（先跑快的）"
    )
    parser.add_argument("--query", default="", help="本轮用户句；缺省用最近一次非「你好」的召回查询")
    parser.add_argument("--from-jshi", action="store_true", help="用 TalkSession 组装与 /prompt 相同的提示词")
    parser.add_argument("--prompt-file", type=Path, default=None, help="已有 {system,user} JSON")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / ".pytest" / "live-loop" / "probe-thinking",
    )
    args = parser.parse_args()

    _load_env()
    _prepare_path()
    os.chdir(ROOT)

    endpoint = (os.getenv("JSHI_MODEL_ENDPOINT") or "").strip()
    api_key = (os.getenv("JSHI_MODEL_API_KEY") or "").strip()
    model = (os.getenv("JSHI_MODEL_NAME") or "").strip()
    if not endpoint or not api_key or not model:
        raise SystemExit("缺少 JSHI_MODEL_ENDPOINT / JSHI_MODEL_API_KEY / JSHI_MODEL_NAME")

    data_dir = Path(os.getenv("JSHI_DATA_DIR") or (ROOT / ".jshi")).resolve()
    query = (args.query or "").strip() or _last_query(
        data_dir, "那这个怎么考证呢？四大名著好像作者都不太能确定或者生平都不清楚"
    )

    if args.prompt_file:
        source = f"file:{args.prompt_file}"
        system, user = _load_prompt_file(args.prompt_file)
    elif args.from_jshi:
        source = "talk_session._prompt_parts"
        system, user = _dump_from_jshi(data_dir, query)
    else:
        source = "reconstructed_smith_zone"
        system, user = _reconstruct_prompt(data_dir, query)

    names = [item.strip() for item in args.variants.split(",") if item.strip()]
    unknown = [name for name in names if name not in VARIANTS]
    if unknown:
        raise SystemExit(f"未知 variant：{unknown}；可选 {list(VARIANTS)}")

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / "prompt.json"
    prompt_path.write_text(
        json.dumps(
            {"source": source, "query": query, "system": system, "user": user},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"model={model}")
    print(f"endpoint={endpoint}")
    print(f"source={source}")
    print(f"query={query}")
    print(f"system_chars={len(system)} user_chars={len(user)} total={len(system) + len(user)}")
    print(f"prompt_file={prompt_path}")
    print()

    runs: list[dict[str, Any]] = []
    for name in names:
        extra = VARIANTS[name]
        print(f"--- {name} extra={extra or '{}'} ---")
        row = _post(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            system=system,
            user=user,
            extra=extra,
            timeout=args.timeout,
        )
        row["variant"] = name
        row["request_extra"] = extra
        runs.append(row)
        if row.get("ok"):
            print(
                f"elapsed={row['elapsed_s']}s  "
                f"content_chars={row['content_chars']}  "
                f"reasoning_chars={row['reasoning_chars']}  "
                f"prompt_tokens={row['prompt_tokens']}  "
                f"completion_tokens={row['completion_tokens']}"
            )
            details = row.get("completion_details")
            if details:
                print(f"completion_tokens_details={details}")
            print(f"content_preview={row['content_preview']!r}")
            if row["reasoning_chars"]:
                print(f"reasoning_preview={row['reasoning_preview']!r}")
        else:
            print(f"FAILED elapsed={row['elapsed_s']}s  {row.get('error')}  {row.get('detail')}")
        print()

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "endpoint": endpoint,
        "source": source,
        "query": query,
        "system_chars": len(system),
        "user_chars": len(user),
        "runs": runs,
    }
    report_path = out_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"report={report_path}")
    return 0 if all(item.get("ok") for item in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
