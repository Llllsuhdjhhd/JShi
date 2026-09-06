"""循环实测：设目标、跑轮次、每轮读库分析。不在循环里改主程序。

用法（仓库根；必须用 conda 环境 py3125，不要用 PATH 里的 python）：

  conda run -n py3125 python tests/live/memory_loop.py --goal "提升回忆质量" --seed 3 --recall 3 --speaker 火星人
  conda run -n py3125 python tests/live/memory_loop.py --scenario tests/live/scenarios/mars-quality.json
  conda run -n py3125 python tests/live/memory_loop.py --scenario tests/live/scenarios/mars-ingest.json --one
  conda run -n py3125 python tests/live/memory_loop.py --continue .pytest/live-loop/<run-id> --one

--one：只跑下一轮然后退出，便于改完代码再继续。
--pause：同进程内每轮结束后等回车；改代码须停掉再 --continue。
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
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]


def _forbid_test_store(data_dir: Path) -> None:
    """火星人与 lux 同一份正式库，落库不得写进测试目录。"""
    resolved = data_dir.resolve()
    for base in (ROOT / ".pytest", ROOT / "tests"):
        try:
            resolved.relative_to(base.resolve())
        except ValueError:
            continue
        raise SystemExit(
            "落库必须用正式目录 .jshi（与 lux 同一份），"
            f"不要写到 {base.name}。"
        )


def _prepare_env(data_dir: Path) -> None:
    os.chdir(ROOT)
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    neighbor = ROOT.parent / "Jshi_memory" / "src"
    if (neighbor / "rems").is_dir():
        path = str(neighbor)
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    os.environ["JSHI_DATA_DIR"] = str(data_dir)
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    os.environ.pop("JSHI_TEST_REMS", None)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        lines.append(text)
    return lines


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def wait_memory(process, subject_id: str, timeout: float = 900.0):
    control = process.memory_control
    deadline = time.time() + timeout
    last_report = 0.0
    while time.time() < deadline:
        inflight = control._inflight.get(subject_id)
        if inflight is None:
            return process.last_memory_control
        future = inflight[2]
        if future.done():
            return control.drain(subject_id)
        elapsed = timeout - (deadline - time.time())
        if elapsed - last_report >= 20:
            last_report = elapsed
            print(f"等待记忆投递… {int(elapsed)}s / {int(timeout)}s", flush=True)
        time.sleep(0.4)
    raise TimeoutError("记忆投递仍未结束")


def _connect(db: Path) -> sqlite3.Connection | None:
    if not db.is_file():
        return None
    con = sqlite3.connect(str(db), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def snapshot_db(db: Path) -> dict[str, Any]:
    empty = {"event_ids": set(), "events": [], "unclosed_count": 0, "unclosed": []}
    con = _connect(db)
    if con is None:
        return empty
    try:
        names = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "events" not in names:
            return empty
        events = []
        for row in con.execute(
            "SELECT event_id, content_raw, role_list, recall_metadata FROM events"
        ):
            roles = json.loads(row["role_list"] or "[]")
            meta = json.loads(row["recall_metadata"] or "{}")
            events.append(
                {
                    "event_id": row["event_id"],
                    "content_raw": row["content_raw"] or "",
                    "roles": roles,
                    "interlocutor": meta.get("interlocutor"),
                    "peer_ids": [
                        entry.get("role_id")
                        for entry in roles
                        if isinstance(entry, dict) and not entry.get("is_subject")
                    ],
                }
            )
        unclosed = []
        unclosed_count = 0
        if "unclosed_events" in names:
            unclosed_count = con.execute(
                "SELECT COUNT(*) FROM unclosed_events"
            ).fetchone()[0]
            for row in con.execute(
                "SELECT id, content_fragments FROM unclosed_events"
            ):
                fragments = row["content_fragments"]
                preview = fragments if isinstance(fragments, str) else json.dumps(
                    fragments, ensure_ascii=False
                )
                unclosed.append(
                    {
                        "id": row["id"],
                        "content": preview or "",
                        "preview": (preview or "")[:500],
                    }
                )
        return {
            "event_ids": {item["event_id"] for item in events},
            "events": events,
            "unclosed_count": unclosed_count,
            "unclosed": unclosed,
        }
    finally:
        con.close()


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def analyze_seed(
    *,
    speaker: str,
    subject_name: str,
    markers: list[str],
    expect_seal: int | None,
    inbound_objects: dict[str, str],
    reply_kind: str | None,
    reply_text: str,
    delivery: dict[str, Any],
    new_events: list[dict[str, Any]],
    unclosed_delta: int,
    unclosed_after: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    checks = []
    mapping_ok = speaker in inbound_objects
    checks.append(
        _check(
            "对象映射",
            mapping_ok,
            f"{speaker} → {inbound_objects.get(speaker, '（无）')}",
        )
    )

    def _has_marker(text: str) -> bool:
        if not markers:
            return False
        return any(marker in (text or "") for marker in markers)

    matched_events = [
        item for item in new_events if _has_marker(item.get("content_raw") or "")
    ]
    unrelated_events = [
        item for item in new_events if item not in matched_events
    ]
    unclosed_after = list(unclosed_after or [])
    unclosed_hit = [
        item
        for item in unclosed_after
        if _has_marker(item.get("content") or item.get("preview") or "")
    ]
    stored_blob = "\n".join(
        item.get("content_raw") or "" for item in matched_events
    )
    if not stored_blob:
        stored_blob = "\n".join(
            item.get("content") or item.get("preview") or "" for item in unclosed_hit
        )
    if expect_seal is None:
        checks.append(
            _check(
                "封存",
                True,
                f"新事件 {len(new_events)} 条（本轮事实 {len(matched_events)}），未闭环变化 {unclosed_delta:+d}",
            )
        )
    else:
        checks.append(
            _check(
                "本轮事实封存",
                len(matched_events) == expect_seal,
                f"期望封存 {expect_seal} 条含本轮事实的事件，实际 {len(matched_events)}；"
                f"无关新封存 {len(unrelated_events)}；未闭环含事实 {len(unclosed_hit)}；"
                f"投递 {delivery.get('status')}/{delivery.get('reason')}",
            )
        )
        checks.append(
            _check(
                "事实已入 09",
                bool(matched_events or unclosed_hit),
                "封存事件" if matched_events else (
                    f"未闭环 { [item.get('id') for item in unclosed_hit] }"
                    if unclosed_hit
                    else "事件与未闭环都没有本轮事实"
                ),
            )
        )
    if unrelated_events:
        checks.append(
            _check(
                "无无关封存",
                False,
                "本轮新封存了与当前事实无关的事件："
                + ",".join(item.get("event_id") or "?" for item in unrelated_events),
            )
        )
    if stored_blob:
        checks.append(
            _check(
                "对话冠名",
                f"{speaker}：" in stored_blob or f"{speaker}:" in stored_blob,
                "含本轮事实的正文是否有说话人冠名",
            )
        )
        if reply_kind == "subject_reply" or (reply_text or "").strip():
            checks.append(
                _check(
                    "主体冠名",
                    f"{subject_name}：" in stored_blob or f"{subject_name}:" in stored_blob,
                    "含本轮事实的正文是否有主体冠名",
                )
            )
        checks.append(
            _check(
                "不是 JSON 状态",
                '"mode": "think"' not in stored_blob and not stored_blob.lstrip().startswith("{"),
                "think/wait 的 JSON 不应进正文",
            )
        )
        if "（动作：" in (reply_text or ""):
            checks.append(
                _check("动作保留", "（动作：" in stored_blob, "回复里的动作应进落库正文")
            )
        peer_source = matched_events or new_events
        peers = [pid for item in peer_source for pid in (item.get("peer_ids") or [])]
        if matched_events:
            checks.append(
                _check(
                    "角色名单",
                    bool(peers),
                    f"peers={peers} interlocutor={[item.get('interlocutor') for item in matched_events]}",
                )
            )
    elif not matched_events and not unclosed_hit:
        checks.append(
            _check(
                "对话冠名",
                False,
                "本轮事实未进入封存也未进入未闭环，无法核对正文",
            )
        )
    for marker in markers:
        haystack = stored_blob or reply_text or ""
        checks.append(
            _check(
                f"事实 {marker}",
                marker in haystack,
                "出现在本轮事实正文或口头里" if marker in haystack else "本轮 09 正文未见",
            )
        )
    return checks


def analyze_recall(
    *,
    markers: list[str],
    recalled_event_ids: list[str],
    memories: list[dict[str, Any]],
    reply_text: str,
    source_errors: list[str],
    question: str = "",
) -> list[dict[str, Any]]:
    checks = []
    checks.append(
        _check(
            "09 召回非空",
            bool(recalled_event_ids or memories),
            f"event_ids={recalled_event_ids or '[]'} fragments={len(memories)}"
            + (f" 源错误={source_errors}" if source_errors else ""),
        )
    )
    memory_blob = "\n".join(str(item.get("content") or "") for item in memories)
    for marker in markers:
        in_mem = marker in memory_blob
        in_reply = marker in (reply_text or "")
        echoed = bool(marker) and marker in (question or "")
        oral = in_reply and not echoed
        checks.append(
            _check(
                f"回忆片段含 {marker}",
                in_mem,
                "装上的 09 片段中出现" if in_mem else "09 片段未见",
            )
        )
        if in_reply and echoed:
            oral_detail = "口头出现，但问句里也有，不能算记起"
        elif oral:
            oral_detail = "口头出现且不是复述问句"
        else:
            oral_detail = "口头未出现"
        checks.append(
            _check(f"口头独立答出 {marker}", oral, oral_detail)
        )
        checks.append(
            _check(
                f"记起 {marker}",
                in_mem or oral,
                "09 片段或口头（非问句复述）" if (in_mem or oral) else "未出现",
            )
        )
    return checks


# DeepSeek V4 Flash，2026-08-17 起官方价；周末全天空闲。单位：元 / 百万 tokens。
FLASH_OFFPEAK_HIT = 0.05
FLASH_OFFPEAK_MISS = 1.5
FLASH_OFFPEAK_OUT = 4.5


def flash_cost_cny(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_tokens: int | None = 0,
) -> float:
    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    cached = int(cached_tokens or 0)
    miss = max(prompt - cached, 0)
    return (miss * FLASH_OFFPEAK_MISS + cached * FLASH_OFFPEAK_HIT + completion * FLASH_OFFPEAK_OUT) / 1_000_000


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def usage_from_metadata(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(meta or {})
    prompt = _as_int(data.get("prompt_tokens"))
    completion = _as_int(data.get("completion_tokens"))
    cached = _as_int(data.get("cached_tokens"))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "cost_cny": round(flash_cost_cny(prompt, completion, cached), 6),
    }


def snapshot_rems_llm(process) -> list[dict[str, Any]]:
    backend = getattr(getattr(process, "memory", None), "_backend", None)
    pipeline = getattr(backend, "_pipeline", None)
    llm = getattr(pipeline, "llm", None)
    history = getattr(llm, "invocation_history", None)
    if history is None:
        return []
    rows = []
    for item in history():
        prompt = getattr(item, "prompt_tokens", None)
        completion = getattr(item, "completion_tokens", None)
        rows.append(
            {
                "task_type": getattr(item, "task_type", ""),
                "model": getattr(item, "model", ""),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cached_tokens": None,
                "cost_cny": round(flash_cost_cny(prompt, completion, 0), 6),
            }
        )
    return rows


def _print_round(row: dict[str, Any]) -> None:
    print("=" * 60, flush=True)
    print(
        f"[{row['phase']} {row['index']}] {row.get('text', '')}",
        flush=True,
    )
    print(
        f"mode={row.get('mode')} reason={row.get('reason') or ''} reply={row.get('reply') or '（无口头）'}",
        flush=True,
    )
    delivery = row.get("delivery") or {}
    print(
        f"投递 {delivery.get('status')} ({delivery.get('reason')})"
        + (f" err={delivery.get('error')}" if delivery.get("error") else ""),
        flush=True,
    )
    for event in row.get("new_events") or []:
        print(f"--- {event.get('event_id')} ---", flush=True)
        print(event.get("content_raw") or "（空正文）", flush=True)
    if row.get("unclosed"):
        print("未闭环：", flush=True)
        for item in row["unclosed"]:
            text = item.get("preview") or item.get("content") or ""
            print(f"  - {item.get('id')}: {text[:200]}", flush=True)
    if row.get("memories"):
        print("装上的回忆：", flush=True)
        for item in row["memories"]:
            print(f"  - {item.get('content', '')[:200]}", flush=True)
    print("检查：", flush=True)
    for check in row.get("checks") or []:
        mark = "通过" if check["ok"] else "未过"
        print(f"  [{mark}] {check['name']}：{check['detail']}", flush=True)
    usage = row.get("usage") or {}
    if usage:
        print(
            f"本轮约 ¥{usage.get('cost_cny', 0)} "
            f"(累计 ¥{usage.get('total_cost_cny', 0)} / 预算 ¥{usage.get('budget_cny', 0)})",
            flush=True,
        )


DEFAULT_MARS = {
    "seed": [
        "我的妹妹叫火卫一，她住在峡谷底，只会用沙尘写信。",
        "火星人过年不放鞭炮，改放三颗冷焰石。",
        "我把尘埃帆寄存在你这里，帆角绣着奥林。",
    ],
    "recall": [
        "我妹妹叫什么？她住在哪里、用什么写信？",
        "你们过年放什么？还放鞭炮吗？",
        "尘埃帆寄存在哪？帆角绣着什么？",
    ],
    "markers": [
        ["火卫一", "峡谷", "沙尘"],
        ["冷焰石", "鞭炮"],
        ["尘埃帆", "奥林"],
    ],
}


def _plan_from_args(args: argparse.Namespace) -> dict[str, Any]:
    scenario: dict[str, Any] = {}
    if args.scenario:
        scenario = _load_json(Path(args.scenario))
    seed = list(scenario.get("seed") or [])
    recall = list(scenario.get("recall") or [])
    markers = [list(item) for item in (scenario.get("markers") or [])]
    speaker = args.speaker or scenario.get("speaker") or "火星人"
    if speaker == "火星人":
        if not seed and args.seed:
            seed = list(DEFAULT_MARS["seed"])
            if not markers:
                markers = [list(item) for item in DEFAULT_MARS["markers"]]
        if not recall and args.recall:
            recall = list(DEFAULT_MARS["recall"])
            if not markers:
                markers = [list(item) for item in DEFAULT_MARS["markers"]]
    if args.seed_file:
        seed = _read_lines(Path(args.seed_file))
    if args.recall_file:
        recall = _read_lines(Path(args.recall_file))
    seed_n = args.seed if args.seed is not None else (len(seed) if seed else 0)
    recall_n = args.recall if args.recall is not None else (len(recall) if recall else 0)
    if seed_n > len(seed):
        raise SystemExit(f"需要 {seed_n} 条写入，材料只有 {len(seed)} 条。用 --seed-file 或 --scenario。")
    if recall_n > len(recall):
        raise SystemExit(f"需要 {recall_n} 条回忆问句，材料只有 {len(recall)} 条。用 --recall-file 或 --scenario。")

    goal = args.goal or scenario.get("goal")
    if not goal:
        raise SystemExit("必须给 --goal，或在 --scenario 里写 goal。")
    flush_each = args.flush_each
    if args.scenario and "flush_each" in scenario and "--flush-each" not in sys.argv and "--no-flush-each" not in sys.argv:
        flush_each = bool(scenario.get("flush_each"))
    expect_seal = args.expect_seal
    if expect_seal is None and "expect_seal" in scenario:
        expect_seal = scenario.get("expect_seal")
    if expect_seal is None and "落库" in goal:
        expect_seal = 1
    if scenario.get("turns"):
        turns = [dict(item) for item in scenario["turns"]]
    else:
        turns = (
            [{"phase": "seed", "text": text, "markers": markers[i] if i < len(markers) else []}
             for i, text in enumerate(seed[:seed_n])]
            + [
                {
                    "phase": "recall",
                    "text": text,
                    "markers": markers[i] if i < len(markers) else [],
                }
                for i, text in enumerate(recall[:recall_n])
            ]
        )
    if not turns:
        raise SystemExit("没有轮次。请给 --seed / --recall，或用 --scenario。")
    budget = args.budget_cny
    if budget is None and "budget_cny" in scenario:
        budget = scenario.get("budget_cny")
    from_now = bool(args.from_now or scenario.get("from_now"))
    return {
        "goal": goal,
        "speaker": speaker,
        "subject_id": args.subject,
        "data_dir": str(Path(args.data_dir).resolve()),
        "flush_each": bool(flush_each),
        "expect_seal": expect_seal,
        "from_now": from_now,
        "budget_cny": budget,
        "pause": bool(args.pause),
        "one": bool(args.one),
        "max_turns": args.max_turns,
        "turns": turns,
        "cursor": 0,
        "rounds": [],
        "cost": {"cognition": [], "rems": [], "total_cny": 0.0},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _save(plan: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    serial = dict(plan)
    path = out_dir / "experiment.json"
    path.write_text(json.dumps(serial, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# {plan['goal']}",
        "",
        f"说话人：{plan['speaker']}　主体：{plan['subject_id']}",
        f"数据目录：{plan['data_dir']}",
        f"flush_each={plan['flush_each']} expect_seal={plan['expect_seal']}",
        f"from_now={plan.get('from_now')} 预算 ¥{plan.get('budget_cny')}",
        "",
    ]
    note = plan.get("from_now_note")
    if note:
        lines.append(f"> {note}")
        lines.append("")
    cost = plan.get("cost") or {}
    lines.append(f"累计约 ¥{cost.get('total_cny', 0)}（Flash 空闲价：命中 0.05 / 未命中 1.5 / 输出 4.5，单位元/百万 tokens；周末按空闲。）")
    lines.append("")
    for row in plan.get("rounds") or []:
        lines.append(f"## {row['phase']} {row['index']}")
        lines.append(f"- 输入：{row.get('text')}")
        lines.append(f"- 口头：{row.get('reply') or '（无）'}")
        lines.append(f"- mode：{row.get('mode')}")
        delivery = row.get("delivery") or {}
        lines.append(f"- 投递：{delivery.get('status')} {delivery.get('reason')}")
        for check in row.get("checks") or []:
            mark = "通过" if check["ok"] else "未过"
            lines.append(f"- [{mark}] {check['name']}：{check['detail']}")
        for event in row.get("new_events") or []:
            lines.append(f"- 事件 {event.get('event_id')}：")
            lines.append("```")
            lines.append(event.get("content_raw") or "")
            lines.append("```")
        for item in row.get("unclosed") or []:
            text = item.get("content") or item.get("preview") or ""
            if any(marker in text for marker in (row.get("markers") or [])):
                lines.append(f"- 未闭环 {item.get('id')}：")
                lines.append("```")
                lines.append(text)
                lines.append("```")
        lines.append("")
    for note in plan.get("cycle_notes") or []:
        lines.append(note)
        lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run_turn(process, plan: dict[str, Any], spec: dict[str, Any], db: Path) -> dict[str, Any]:
    from jshi.experienceledger import OutputKind

    subject_id = plan["subject_id"]
    speaker = plan["speaker"]
    before = snapshot_db(db)
    result = process.experience(subject_id, spec["text"], object_ref=speaker)
    memory_result = wait_memory(process, subject_id)
    after = snapshot_db(db)
    new_ids = after["event_ids"] - before["event_ids"]
    new_events = [item for item in after["events"] if item["event_id"] in new_ids]
    segments = list(process.activity_ledger.list_experiences(subject_id))
    inbound = next(
        item
        for item in reversed(segments)
        if item.output_kind is OutputKind.EXTERNAL_INPUT
    )
    outgoing = next(
        (
            item
            for item in reversed(segments)
            if item.output_kind
            in {OutputKind.SUBJECT_REPLY, OutputKind.SUBJECT_STATE}
        ),
        None,
    )
    memories = [
        {
            "id": getattr(frag, "id", ""),
            "source": getattr(frag, "source", ""),
            "content": getattr(frag, "content", ""),
            "object_id": getattr(frag, "object_id", None),
        }
        for frag in result.current_state.fragments
        if getattr(frag, "source", None) == "memory"
    ]
    recalled_event_ids = [
        getattr(item, "event_id", "")
        for item in result.current_state.recalled
        if getattr(item, "event_id", "")
    ]
    source_errors = [
        f"{report.source}:{report.error}"
        for report in result.current_state.source_report
        if getattr(report, "error", None)
    ]
    delivery = {
        "status": getattr(memory_result, "status", None),
        "reason": getattr(getattr(memory_result, "decision", None), "reason", None),
        "error": getattr(memory_result, "error", None),
    }
    reply_kind = outgoing.output_kind.value if outgoing else None
    reply_text = result.action_text or ""
    markers = list(spec.get("markers") or [])
    if spec["phase"] == "recall":
        checks = analyze_recall(
            markers=markers,
            recalled_event_ids=recalled_event_ids,
            memories=memories,
            reply_text=reply_text,
            source_errors=source_errors,
            question=spec["text"],
        )
    else:
        identity_name = "匠石"
        try:
            identity_name = process.identities.get(subject_id).name or "匠石"
        except KeyError:
            pass
        checks = analyze_seed(
            speaker=speaker,
            subject_name=identity_name,
            markers=markers,
            expect_seal=plan.get("expect_seal"),
            inbound_objects=dict(inbound.objects or {}),
            reply_kind=reply_kind,
            reply_text=reply_text,
            delivery=delivery,
            new_events=new_events,
            unclosed_delta=after["unclosed_count"] - before["unclosed_count"],
            unclosed_after=after.get("unclosed") or [],
        )
    return {
        "phase": spec["phase"],
        "index": spec["index"],
        "text": spec["text"],
        "markers": markers,
        "speaker_id": result.speaker.object_id,
        "speaker_label": result.speaker.label,
        "mode": result.response_plan.mode,
        "reason": result.response_plan.reason,
        "reply": reply_text,
        "reply_kind": reply_kind,
        "inbound_objects": dict(inbound.objects or {}),
        "delivery": delivery,
        "new_events": new_events,
        "unclosed": after.get("unclosed") or [],
        "unclosed_count": after["unclosed_count"],
        "memories": memories,
        "recalled_event_ids": recalled_event_ids,
        "source_errors": source_errors,
        "checks": checks,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="循环实测记忆写入与回忆。每轮读 rems.db，写出分析。",
        epilog=(
            "例：提升回忆质量，观察 3 轮再回忆 3 轮：\n"
            "  python tests/live/memory_loop.py --scenario tests/live/scenarios/mars-quality.json\n"
            "例：落库 3 轮、每轮看结果再改：\n"
            "  python tests/live/memory_loop.py --scenario tests/live/scenarios/mars-ingest.json --one\n"
            "  python tests/live/memory_loop.py --continue .pytest/live-loop/<id> --one"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--goal", help="本轮实验目的（scenario 里也可写）")
    parser.add_argument("--scenario", help="JSON：goal / speaker / seed / recall / markers")
    parser.add_argument("--continue", dest="continue_dir", help="已有 experiment.json 的目录，接着跑")
    parser.add_argument("--speaker", help="说话人名字，默认火星人")
    parser.add_argument("--subject", default="stone")
    parser.add_argument("--data-dir", default=".jshi")
    parser.add_argument("--seed", type=int, help="写入轮数")
    parser.add_argument("--recall", type=int, help="回忆轮数")
    parser.add_argument("--seed-file", help="写入原话，一行一句")
    parser.add_argument("--recall-file", help="回忆问句，一行一句")
    parser.add_argument(
        "--flush-each",
        dest="flush_each",
        action="store_true",
        help="每轮收尾即投递（段数门槛改为 2）。默认开启。",
    )
    parser.add_argument(
        "--no-flush-each",
        dest="flush_each",
        action="store_false",
        help="不改冲刷门槛，沿用正式默认（约 20 段）",
    )
    parser.set_defaults(flush_each=True)
    parser.add_argument("--expect-seal", type=int, help="每轮期望新封存事件数；落库目标默认 1")
    parser.add_argument("--one", action="store_true", help="只跑下一轮然后退出")
    parser.add_argument(
        "--max-turns",
        type=int,
        help="本趟最多跑几轮（例如 2 = 一次写入 + 一次回忆）",
    )
    parser.add_argument(
        "--from-now",
        action="store_true",
        help="把记忆游标推到账本末端，本实验不投递此前积压",
    )
    parser.add_argument(
        "--budget-cny",
        type=float,
        help="经济阈值（元，DeepSeek Flash 空闲价估算）",
    )
    parser.add_argument("--pause", action="store_true", help="每轮结束后等回车")
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="不触发 30 投递（积压很大、REMS 会长时间不返回时用）",
    )
    parser.add_argument("--out", help="报告目录，默认 .pytest/live-loop/<时间>")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.continue_dir:
        out_dir = Path(args.continue_dir)
        plan = _load_json(out_dir / "experiment.json")
        plan["one"] = bool(args.one)
        plan["pause"] = bool(args.pause)
        if args.max_turns is not None:
            plan["max_turns"] = args.max_turns
        if args.budget_cny is not None:
            plan["budget_cny"] = args.budget_cny
        if args.from_now:
            plan["from_now"] = True
        if args.skip_ingest:
            plan["skip_ingest"] = True
    else:
        plan = _plan_from_args(args)
        out_dir = Path(args.out) if args.out else ROOT / ".pytest" / "live-loop" / _now_id()
        plan["out_dir"] = str(out_dir.resolve())
        _save(plan, out_dir)

    plan.setdefault("cost", {"cognition": [], "rems": [], "total_cny": 0.0})

    data_dir = Path(plan["data_dir"])
    _forbid_test_store(data_dir)
    _prepare_env(data_dir)
    from jshi.app.cli import _load_local_env, _runtime
    from jshi.experienceledger.port import ConsumerKind

    _load_local_env()
    try:
        process, _identities, _subjects = _runtime(data_dir)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "AlreadyLocked" in type(exc).__name__ or "Permission denied" in message:
            print(
                "rems 的 qdrant 被占用。先关掉 talk 或其他占用 .jshi/rems/qdrant 的进程。",
                file=sys.stderr,
            )
        raise
    if plan.get("from_now") and not plan.get("from_now_applied"):
        ledger = process.activity_ledger
        subject_id = plan["subject_id"]
        before = ledger.consumer_cursor(subject_id, ConsumerKind.MEMORY)
        head = ledger.head_sequence(subject_id)
        ledger.advance_consumer_cursor(subject_id, ConsumerKind.MEMORY, head)
        plan["from_now_applied"] = True
        plan["from_now_note"] = (
            f"记忆游标 {before} → {head}。此前账本段仍在，本实验不把积压交 09。"
        )
        print(plan["from_now_note"], flush=True)
        _save(plan, out_dir)
    if plan.get("skip_ingest"):
        control = process.memory_control
        control._flush_max_segments = 10**9
        control._flush_max_chars = 10**12
        control._flush_max_idle_seconds = 10**9
        control._flush_on_idle_seconds = 10**9
        control._flush_night_window = ""
        print("本趟不触发投递（只跑对话与回忆检查）。", flush=True)
    elif plan.get("flush_each"):
        process.memory_control._flush_max_segments = 2
        print("已把本进程冲刷段数改为 2（只影响这一次脚本，不改默认 20）。", flush=True)

    db = data_dir / "rems" / "rems.db"
    remaining = plan["turns"][plan["cursor"] :]
    if not remaining:
        print("没有剩余轮次。", flush=True)
        print(f"报告 {out_dir / 'report.md'}", flush=True)
        return 0
    print(f"目标：{plan['goal']}", flush=True)
    print(f"报告：{out_dir}", flush=True)

    rems_seen = len(snapshot_rems_llm(process))
    budget = plan.get("budget_cny")
    max_turns = plan.get("max_turns")
    ran = 0
    try:
        for offset, spec in enumerate(remaining):
            spec = dict(spec)
            spec["index"] = plan["cursor"] + 1
            row = run_turn(process, plan, spec, db)
            cognition = usage_from_metadata(
                getattr(getattr(process, "last_model_response", None), "metadata", None)
            )
            rems_now = snapshot_rems_llm(process)
            rems_delta = rems_now[rems_seen:]
            rems_seen = len(rems_now)
            turn_cost = float(cognition.get("cost_cny") or 0) + sum(
                float(item.get("cost_cny") or 0) for item in rems_delta
            )
            plan["cost"]["cognition"].append(cognition)
            plan["cost"]["rems"].extend(rems_delta)
            plan["cost"]["total_cny"] = round(
                float(plan["cost"].get("total_cny") or 0) + turn_cost, 6
            )
            row["usage"] = {
                **cognition,
                "rems": rems_delta,
                "cost_cny": round(turn_cost, 6),
                "total_cost_cny": plan["cost"]["total_cny"],
                "budget_cny": budget,
            }
            plan["rounds"].append(row)
            plan["cursor"] += 1
            ran += 1
            _print_round(row)
            _save(plan, out_dir)
            if budget is not None and plan["cost"]["total_cny"] >= float(budget):
                print(
                    f"已达经济阈值 ¥{budget}（累计约 ¥{plan['cost']['total_cny']}），停止。",
                    flush=True,
                )
                break
            if plan.get("one"):
                break
            if max_turns is not None and ran >= int(max_turns):
                break
            if plan.get("pause") and offset + 1 < len(remaining):
                input("看完本轮后可改配置。同进程改代码不会生效。回车继续，Ctrl+C 停。")
    except KeyboardInterrupt:
        print("已中断，进度已写入报告。", flush=True)
        _save(plan, out_dir)
        return 130

    left = len(plan["turns"]) - plan["cursor"]
    print("-" * 60, flush=True)
    print(
        f"本趟跑了 {ran} 轮，还剩 {left} 轮。累计约 ¥{plan['cost'].get('total_cny', 0)}。",
        flush=True,
    )
    if left:
        print(
            f"继续：conda run -n py3125 python tests/live/memory_loop.py --continue {out_dir} --max-turns 2",
            flush=True,
        )
    print(f"报告 {out_dir / 'report.md'}", flush=True)
    failed = [
        check
        for row in plan["rounds"]
        for check in row.get("checks") or []
        if not check.get("ok")
    ]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
