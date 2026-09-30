"""LoCoMo-Plus 认知记忆测评：把长对话回放进匠石，再用触发句走主流程，由模型裁判评分。

说明文档：doc/LoCoMo-Plus测评.md。子命令：

    plan   选题，写 plan.json
    base   回放一段 LoCoMo 对话的前若干场，存快照（同一对话的题共用）
    item   从快照接着回放：插入线索、补完其后各场、整理人物经验与肖像
    ask    在某一条件下（关掉哪些装载源）问触发句，存回复
    judge  按论文的 Cognitive 裁判提示词评分
    report 汇总各条件得分与费用
    run    以上各步按顺序跑完，已完成的跳过

回放、提问都在各自子进程与独立数据目录里进行，不碰 .jshi 主库和正在运行的对话。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_WORK = ROOT / ".jshi" / "locomo-plus"
DEFAULT_DATA = ROOT.parent / "Locomo-Plus" / "data"

SUBJECT = "stone"
SUBJECT_NAME = "匠石"
TURN_GAP = timedelta(seconds=20)
QUERY_DELAY = timedelta(days=7)
SESSION_IDLE = timedelta(hours=1)
RELATION_TYPES = ("causal", "state", "goal", "value")

# 条件 → 提问时去掉的装载源（assembly source 的 name）。
CONDITIONS: dict[str, tuple[str, ...]] = {
    "full": (),
    "no_experience": ("person_experience",),
    "no_person": ("person_experience", "person_portrait"),
    "no_memory": ("person_experience", "person_portrait", "memory"),
}
DEFAULT_CONDITIONS = ("full", "no_experience", "no_person", "no_memory")

# 元 / 百万 token；按 DeepSeek 标价估算，缓存命中不单算。
PRICE_PROMPT = float(os.environ.get("LOCOMO_PRICE_PROMPT", "2.1"))
PRICE_COMPLETION = float(os.environ.get("LOCOMO_PRICE_COMPLETION", "8.4"))

# 与 Locomo-Plus/evaluation_framework/task_eval/prompt.py 的 Cognitive 模板逐字一致。
COGNITIVE_JUDGE_PROMPT = """
You are a Memory Awareness Judge.
Your task: Judge whether the Model Prediction considers or is linked to the Evidence. If there is a clear connection, the answer is correct (score 1); if not, it is wrong (no score).

Labels:
- "correct": The prediction explicitly or implicitly reflects/uses the evidence (memory or constraint). Give 1 point.
- "wrong": The prediction does not show such a link to the evidence. No point.

Memory/Evidence:
{evidence}

Model Prediction:
{pred}

Return your judgment strictly in JSON format:
{{"label": "correct"|"wrong", "reason": "<Does the prediction relate to the evidence?>"}}
"""


# --------------------------------------------------------------------------- #
# 数据：解析与拼接（与 Locomo-Plus/data/build_conv.py 同一算法）
# --------------------------------------------------------------------------- #

_NUM_WORD = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def parse_time_gap(time_gap: str) -> int:
    s = (time_gap or "").lower().strip()
    m = re.search(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a|an)\b\s*"
        r"(week|weeks|month|months|year|years)\b",
        s,
    )
    if not m:
        return 0
    num, unit = m.groups()
    if num.isdigit():
        count = int(num)
    elif num in {"a", "an"}:
        count = 1
    else:
        count = _NUM_WORD.get(num, 0)
    if unit.startswith("week"):
        return count * 7
    if unit.startswith("month"):
        return count * 30
    return count * 365


def parse_ab_dialogue(text: str) -> list[tuple[str, str]]:
    turns = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if line.startswith("A:"):
            turns.append(("A", line[2:].strip()))
        elif line.startswith("B:"):
            turns.append(("B", line[2:].strip()))
    return turns


def parse_session_time(raw: str) -> datetime:
    return datetime.strptime(raw, "%I:%M %p on %d %B, %Y")


@dataclass(frozen=True)
class Session:
    when: datetime
    turns: tuple[tuple[str, str], ...]  # (A|B, 文本)


def conversation_sessions(conv: dict) -> tuple[str, str, list[Session]]:
    speaker_a, speaker_b = conv["speaker_a"], conv["speaker_b"]
    sessions = []
    idx = 1
    while f"session_{idx}" in conv:
        turns = []
        for turn in conv[f"session_{idx}"]:
            side = "A" if turn.get("speaker") == speaker_a else "B"
            text = (turn.get("text") or "").strip()
            caption = (turn.get("blip_caption") or "").strip()
            if caption:
                text = f"{text}（分享了一张图片：{caption}）".strip()
            if text:
                turns.append((side, text))
        sessions.append(Session(parse_session_time(conv[f"session_{idx}_date_time"]), tuple(turns)))
        idx += 1
    return speaker_a, speaker_b, sessions


def load_dataset(data_dir: Path) -> tuple[list[dict], list[dict]]:
    plus = json.loads((data_dir / "locomo_plus.json").read_text(encoding="utf-8"))
    locomo = json.loads((data_dir / "locomo10.json").read_text(encoding="utf-8"))
    return plus, locomo


def plan_item(index: int, plus_item: dict, locomo: list[dict]) -> dict:
    """同 unified_input.py：第 i 条挂在第 i % 10 段对话上；线索按 time_gap 倒推插入。"""
    conv_index = index % len(locomo)
    speaker_a, speaker_b, sessions = conversation_sessions(locomo[conv_index]["conversation"])
    query_time = sessions[-1].when + QUERY_DELAY
    cue_time = query_time - timedelta(days=parse_time_gap(plus_item.get("time_gap", "")))
    cue_after = sum(1 for s in sessions if s.when <= cue_time)
    cue_turns = parse_ab_dialogue(plus_item["cue_dialogue"])
    names = {"A": speaker_a, "B": speaker_b}
    evidence = "\n".join(f"{names[side]}：{text}" for side, text in cue_turns)
    trigger = " ".join(text for side, text in parse_ab_dialogue(plus_item["trigger_query"]) if side == "A")
    return {
        "id": f"p{index:03d}",
        "index": index,
        "relation_type": plus_item.get("relation_type", ""),
        "time_gap": plus_item.get("time_gap", ""),
        "conv": conv_index,
        "speaker_a": speaker_a,
        "speaker_b": speaker_b,
        "sessions_total": len(sessions),
        "cue_after_sessions": cue_after,
        "cue_time": cue_time.isoformat(),
        "query_time": query_time.isoformat(),
        "cue_dialogue": plus_item["cue_dialogue"],
        "trigger": trigger,
        "evidence": evidence,
    }


def object_id_for(conv_index: int) -> str:
    return f"OBJ-locomo-conv{conv_index:02d}-a"


# --------------------------------------------------------------------------- #
# 目录与文件
# --------------------------------------------------------------------------- #


class Layout:
    def __init__(self, work: Path) -> None:
        self.work = work

    @property
    def plan(self) -> Path:
        return self.work / "plan.json"

    def base_dir(self, conv: int) -> Path:
        return self.work / "base" / f"conv{conv:02d}"

    def snapshot(self, conv: int, sessions: int) -> Path:
        return self.base_dir(conv) / f"snap{sessions:02d}"

    def base_meta(self, conv: int) -> Path:
        return self.base_dir(conv) / "meta.json"

    def item_dir(self, item_id: str) -> Path:
        return self.work / "items" / item_id

    def item_ready(self, item_id: str) -> Path:
        return self.item_dir(item_id) / "ready"

    def item_meta(self, item_id: str) -> Path:
        return self.item_dir(item_id) / "item.json"

    def ask_dir(self, item_id: str, cond: str) -> Path:
        return self.item_dir(item_id) / cond

    def answer(self, item_id: str, cond: str) -> Path:
        return self.item_dir(item_id) / f"answer-{cond}.json"

    def judge(self, item_id: str, cond: str) -> Path:
        return self.item_dir(item_id) / f"judge-{cond}.json"

    def logs(self) -> Path:
        path = self.work / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def checkpoint_sqlite(folder: Path) -> None:
    """复制前把 WAL 并回主库，快照里只需主库文件。"""
    for db in folder.rglob("*.sqlite*"):
        _checkpoint(db)
    for db in folder.rglob("*.db"):
        _checkpoint(db)


def _checkpoint(db: Path) -> None:
    if db.suffix in {".db-wal", ".db-shm"} or db.name.endswith(("-wal", "-shm")):
        return
    try:
        con = sqlite3.connect(str(db), timeout=30)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    except sqlite3.Error:
        pass


def copy_data_dir(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".lock", "__pycache__"))


# --------------------------------------------------------------------------- #
# 匠石运行环境（只在子进程里调用）
# --------------------------------------------------------------------------- #


def setup_env(data_dir: Path, work: Path) -> None:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / "src"))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    os.environ["JSHI_DATA_DIR"] = str(data_dir)
    os.environ["JSHI_MEMORY_BACKEND"] = "rems3"
    os.environ["JSHI_REMS_DATA_DIR"] = str(data_dir / "rems")
    os.environ["JSHI_TOOL_ENGINE"] = "stub"
    os.environ.setdefault("REMS_LOG_DIR", str(work / "logs" / "llm_failures"))
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    from jshi.app.cli import _load_local_env

    _load_local_env()


def init_data_dir(data_dir: Path, item_or_conv: dict) -> None:
    """全新数据目录：主体匠石 + 已确认的对象 A。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        data_dir / "identities.json",
        [{
            "subject_id": SUBJECT,
            "name": SUBJECT_NAME,
            "origin": "基础型匠石",
            "narrative": "我是匠石，从人类文明的共同基础出发继续成长。",
            "revision": 1,
        }],
    )
    from jshi.recognition import ObjectProfileRepository
    from jshi.recognition.profile import ObjectProfile

    profiles = ObjectProfileRepository(data_dir / "subject.sqlite3")
    oid = object_id_for(item_or_conv["conv"])
    if profiles.get(oid) is None:
        profiles.create(ObjectProfile(
            object_id=oid,
            label=item_or_conv["speaker_a"],
            source="locomo-plus",
            status="confirmed",
        ))


class Replayer:
    """把 A/B 原话按模拟时间写进经历账本，并按真实触发规则投递给 09。"""

    def __init__(self, data_dir: Path, conv: int, speaker_a: str) -> None:
        from jshi.app.cli import _runtime

        self.process, _identities, _subjects = _runtime(data_dir)
        self.backend = self.process.memory._backend
        self.ledger = self.process.activity_ledger
        self.oid = object_id_for(conv)
        self.objects = {speaker_a: self.oid}
        self.clock = [datetime.now().astimezone()]
        control = self.process.memory_control
        control._now = lambda: self.clock[0]
        control._flush_night_window = ""
        self.failures: list[str] = []

    def llm_history(self) -> list:
        llm = getattr(getattr(self.backend, "_pipeline", None), "llm", None)
        return list(llm.invocation_history()) if llm is not None else []

    def replay(self, turns: list[tuple[str, str]], start: datetime) -> None:
        when = start
        for side, text in turns:
            self.clock[0] = when
            if side == "A":
                self.ledger.append_external(
                    SUBJECT,
                    actor_object_id=self.oid,
                    text_raw=text,
                    objects=self.objects,
                    mentioned_object_ids=(self.oid,),
                    occurred_at=when,
                )
            else:
                self.ledger.append_subject_reply(
                    SUBJECT,
                    text_raw=text,
                    objects=self.objects,
                    mentioned_object_ids=(self.oid,),
                    occurred_at=when,
                )
            self._flush_due()
            when += TURN_GAP
        # 场与场之间相隔数日：按空闲触发把本场剩余部分投递掉。
        self.clock[0] = when + SESSION_IDLE
        self._flush_due()

    def _flush_due(self) -> None:
        control = self.process.memory_control
        while True:
            result = control.run_once(SUBJECT)
            if result.status == "failed":
                self.failures.append(result.error or "ingest failed")
                continue
            if result.decision.reason == "max_retry_reached":
                raise RuntimeError(f"记忆投递重试用尽：{self.failures[-1] if self.failures else ''}")
            if result.status != "ingested":
                return

    def wait_refresh(self, timeout: float = 1800) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            workers = [t for t in threading.enumerate() if t.name == "jshi-person-experience" and t.is_alive()]
            if not workers:
                return
            for worker in workers:
                worker.join(timeout=max(1.0, deadline - time.time()))
        raise TimeoutError("人物经验后台维护未在时限内结束")

    def settle(self) -> None:
        """等后台维护做完，再把剩余白描全部吸收进相处经验和肖像。"""
        self.wait_refresh()
        pipeline = self.backend._pipeline
        config = pipeline.config
        config.experience_person_trigger_count = 1
        config.portrait_white_painting_trigger_count = 1
        experience = self.backend.long_term_experience
        if experience is not None:
            experience.refresh_person_experience(SUBJECT, self.oid)
        portrait = getattr(self.backend, "_portrait_refresh", None)
        if portrait is not None:
            portrait.refresh(SUBJECT, self.oid, force=True)

    def state(self) -> dict:
        experience = ""
        exp = self.backend.long_term_experience
        if exp is not None:
            hits = exp.recall_experience(SUBJECT, "", object_ids=(self.oid,), budget_chars=100000)
            experience = hits[0].content if hits else ""
        portrait = self.backend.portrait(SUBJECT, self.oid) or {}
        rems_db = Path(os.environ["JSHI_REMS_DATA_DIR"]) / "rems.db"
        counts = {}
        try:
            con = sqlite3.connect(f"file:{rems_db.as_posix()}?mode=ro", uri=True)
            for table in ("events", "white_painting_entries", "person_experience_evidence"):
                try:
                    counts[table] = con.execute(f"select count(*) from {table}").fetchone()[0]
                except sqlite3.Error:
                    counts[table] = None
            con.close()
        except sqlite3.Error:
            pass
        return {
            "experience": experience,
            "portrait": portrait.get("visible_summary", "") if isinstance(portrait, dict) else "",
            "portrait_levels": portrait.get("levels", {}) if isinstance(portrait, dict) else {},
            "rems_counts": counts,
            "ingest_failures": list(self.failures),
        }


def usage_summary(history: list) -> dict:
    out: dict[str, dict] = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    for item in history:
        row = out[getattr(item, "task_type", "") or "unknown"]
        row["calls"] += 1
        row["prompt_tokens"] += int(getattr(item, "prompt_tokens", 0) or 0)
        row["completion_tokens"] += int(getattr(item, "completion_tokens", 0) or 0)
    return dict(out)


def session_start(session_when: datetime, offset: timedelta) -> datetime:
    return (session_when + offset).astimezone()


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #


def cmd_plan(args) -> int:
    plus, locomo = load_dataset(args.data)
    items = [plan_item(i, entry, locomo) for i, entry in enumerate(plus)]
    if args.items:
        wanted = {f"p{int(x):03d}" if x.isdigit() else x for x in args.items.split(",")}
        chosen = [item for item in items if item["id"] in wanted]
    else:
        rng = random.Random(args.seed)
        chosen = []
        for relation in RELATION_TYPES:
            pool = [item for item in items if item["relation_type"] == relation]
            if args.per_type and args.per_type < len(pool):
                pool = rng.sample(pool, args.per_type)
            chosen.extend(sorted(pool, key=lambda item: item["index"]))
    plan = {
        "created_at": datetime.now().astimezone().isoformat(),
        "data_dir": str(args.data),
        "seed": args.seed,
        "per_type": args.per_type,
        "conditions": list(args.conditions),
        "items": chosen,
    }
    layout = Layout(args.work)
    write_json(layout.plan, plan)
    need = defaultdict(set)
    for item in chosen:
        need[item["conv"]].add(item["cue_after_sessions"])
    tail = sum(item["sessions_total"] - item["cue_after_sessions"] for item in chosen)
    base = sum(max(snaps) for snaps in need.values())
    print(f"选了 {len(chosen)} 条，涉及 {len(need)} 段对话；")
    print(f"共用前缀需回放 {base} 场，各题线索之后另需回放 {tail} 场；条件 {len(args.conditions)} 个。")
    print(f"写入 {layout.plan}")
    return 0


def cmd_base(args) -> int:
    layout = Layout(args.work)
    plan = read_json(layout.plan)
    items = [item for item in plan["items"] if item["conv"] == args.conv]
    if not items:
        print(f"计划里没有挂在对话 {args.conv} 上的题")
        return 0
    needed = sorted({item["cue_after_sessions"] for item in items})
    missing = [n for n in needed if not (layout.snapshot(args.conv, n) / "done").exists()]
    if not missing:
        print(f"对话 {args.conv} 的快照已齐：{needed}")
        return 0
    plus, locomo = load_dataset(Path(plan["data_dir"]))
    speaker_a, _speaker_b, sessions = conversation_sessions(locomo[args.conv]["conversation"])
    meta_path = layout.base_meta(args.conv)
    if meta_path.exists():
        meta = read_json(meta_path)
    else:
        anchor = datetime.now()
        meta = {
            "conv": args.conv,
            "speaker_a": speaker_a,
            "anchor": anchor.isoformat(),
            "offset_seconds": (anchor - (sessions[-1].when + QUERY_DELAY)).total_seconds(),
            "snapshots": {},
        }
        write_json(meta_path, meta)
    offset = timedelta(seconds=meta["offset_seconds"])

    existing = sorted(
        n for n in range(0, min(missing) + 1)
        if (layout.snapshot(args.conv, n) / "done").exists()
    )
    start = existing[-1] if existing else 0
    work_dir = layout.base_dir(args.conv) / "work"
    if existing:
        copy_data_dir(layout.snapshot(args.conv, start), work_dir)
    elif work_dir.exists():
        shutil.rmtree(work_dir)

    setup_env(work_dir, args.work)
    init_data_dir(work_dir, {"conv": args.conv, "speaker_a": speaker_a})
    replayer = Replayer(work_dir, args.conv, speaker_a)
    started = time.perf_counter()
    for count in range(start, max(missing) + 1):
        if count > start:
            session = sessions[count - 1]
            replayer.replay(list(session.turns), session_start(session.when, offset))
            print(f"对话 {args.conv}：已回放 {count}/{len(sessions)} 场", flush=True)
        if count in missing:
            replayer.wait_refresh()
            checkpoint_sqlite(work_dir)
            target = layout.snapshot(args.conv, count)
            copy_data_dir(work_dir, target)
            (target / "done").write_text(datetime.now().isoformat(), encoding="utf-8")
            meta["snapshots"][str(count)] = {
                "saved_at": datetime.now().isoformat(),
                "usage_so_far": usage_summary(replayer.llm_history()),
            }
            write_json(meta_path, meta)
    meta.setdefault("runs", []).append({
        "from": start,
        "to": max(missing),
        "seconds": round(time.perf_counter() - started, 1),
        "usage": usage_summary(replayer.llm_history()),
        "ingest_failures": replayer.failures,
    })
    write_json(meta_path, meta)
    return 0


def cmd_item(args) -> int:
    layout = Layout(args.work)
    plan = read_json(layout.plan)
    item = next(entry for entry in plan["items"] if entry["id"] == args.item)
    if layout.item_meta(item["id"]).exists():
        print(f"{item['id']} 已准备好")
        return 0
    snap = layout.snapshot(item["conv"], item["cue_after_sessions"])
    if not (snap / "done").exists():
        raise SystemExit(f"缺少快照 {snap}，先跑 base --conv {item['conv']}")
    meta = read_json(layout.base_meta(item["conv"]))
    offset = timedelta(seconds=meta["offset_seconds"])
    plus, locomo = load_dataset(Path(plan["data_dir"]))
    _a, _b, sessions = conversation_sessions(locomo[item["conv"]]["conversation"])

    ready = layout.item_ready(item["id"])
    copy_data_dir(snap, ready)
    setup_env(ready, args.work)
    replayer = Replayer(ready, item["conv"], item["speaker_a"])
    started = time.perf_counter()
    cue_turns = parse_ab_dialogue(item["cue_dialogue"])
    replayer.replay(cue_turns, session_start(datetime.fromisoformat(item["cue_time"]), offset))
    for session in sessions[item["cue_after_sessions"]:]:
        replayer.replay(list(session.turns), session_start(session.when, offset))
    replayer.settle()
    state = replayer.state()
    checkpoint_sqlite(ready)
    write_json(layout.item_meta(item["id"]), {
        **item,
        "prepared_at": datetime.now().isoformat(),
        "seconds": round(time.perf_counter() - started, 1),
        "usage": usage_summary(replayer.llm_history()),
        **state,
    })
    print(f"{item['id']} 准备完毕：相处经验 {len(state['experience'])} 字，肖像 {len(state['portrait'])} 字")
    return 0


def cmd_ask(args) -> int:
    layout = Layout(args.work)
    item = read_json(layout.item_meta(args.item))
    if layout.answer(args.item, args.cond).exists():
        print(f"{args.item}/{args.cond} 已有回复")
        return 0
    drop = set(CONDITIONS[args.cond])
    data_dir = layout.ask_dir(args.item, args.cond)
    copy_data_dir(layout.item_ready(args.item), data_dir)
    os.environ["JSHI_EXPERIENCE_EXIT_WAIT_S"] = "0"
    setup_env(data_dir, args.work)
    from jshi.app.cli import _runtime
    from jshi.models.prompt import build_user

    process, _identities, _subjects = _runtime(data_dir)
    process.memory_control._flush_night_window = ""
    # 只要回复；写场与本测无关，省一次调用。
    process.write_zone = None
    process.assembler.sources = tuple(
        source for source in process.assembler.sources if getattr(source, "name", "") not in drop
    )
    captured: list[str] = []
    cognition = process.cognition
    for name in ("generate", "generate_stream"):
        original = getattr(cognition, name, None)
        if original is None:
            continue

        def wrapper(request, *a, _original=original, **kw):
            if getattr(request, "purpose", "") == "subject_activity":
                captured.append(build_user(request))
            return _original(request, *a, **kw)

        setattr(cognition, name, wrapper)

    started = time.perf_counter()
    result = process.experience(SUBJECT, item["trigger"], object_ref=item["speaker_a"])
    seconds = round(time.perf_counter() - started, 1)
    response = process.last_model_response
    metadata = dict(getattr(response, "metadata", None) or {})
    fragments = result.current_state.fragments
    write_json(layout.answer(args.item, args.cond), {
        "id": args.item,
        "condition": args.cond,
        "dropped_sources": sorted(drop),
        "trigger": item["trigger"],
        "reply": result.action_text or "",
        "mode": getattr(result.response_plan, "mode", ""),
        "seconds": seconds,
        "loaded": {
            source: sum(len(f.content) for f in fragments if f.source == source)
            for source in ("person_experience", "person_portrait", "memory")
        },
        "prompt_chars": len(captured[0]) if captured else 0,
        "prompt": captured[0] if captured else "",
        "usage": {
            "prompt_tokens": int(metadata.get("prompt_tokens") or 0),
            "completion_tokens": int(metadata.get("completion_tokens") or 0),
        },
    })
    # 本进程的数据目录只为这一问，答完即删，保留回复文件。
    if not args.keep:
        shutil.rmtree(data_dir, ignore_errors=True)
    print(f"{args.item}/{args.cond}：{(result.action_text or '')[:80]}")
    return 0


def _judge_client():
    sys.path.insert(0, str(ROOT / "src"))
    from jshi.app.cli import _load_local_env
    from jshi.memory.rems3 import openai_compat_base_url
    from openai import OpenAI

    _load_local_env()
    endpoint = os.environ.get("LOCOMO_JUDGE_ENDPOINT") or os.environ.get("JSHI_MODEL_ENDPOINT", "")
    key = os.environ.get("LOCOMO_JUDGE_API_KEY") or os.environ.get("JSHI_MODEL_API_KEY", "")
    model = os.environ.get("LOCOMO_JUDGE_MODEL") or os.environ.get("JSHI_MODEL_NAME", "")
    if not (endpoint and key and model):
        raise SystemExit("裁判模型未配置：需要 LOCOMO_JUDGE_* 或 JSHI_MODEL_ENDPOINT / API_KEY / NAME")
    return OpenAI(base_url=openai_compat_base_url(endpoint), api_key=key), model


def parse_judge(raw: str) -> tuple[str, str]:
    raw = (raw or "").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            return str(obj.get("label", "")).strip().lower(), str(obj.get("reason", "")).strip()
        except json.JSONDecodeError:
            pass
    lowered = raw.lower()
    if "correct" in lowered:
        return "correct", raw[:200]
    if "wrong" in lowered:
        return "wrong", raw[:200]
    return "", raw[:200]


def judge_one(client, model: str, layout: Layout, item_id: str, cond: str) -> dict:
    item = read_json(layout.item_meta(item_id))
    answer = read_json(layout.answer(item_id, cond))
    prompt = COGNITIVE_JUDGE_PROMPT.format(evidence=item["evidence"], pred=answer["reply"])
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=512,
    )
    raw = completion.choices[0].message.content or ""
    label, reason = parse_judge(raw)
    usage = getattr(completion, "usage", None)
    record = {
        "id": item_id,
        "condition": cond,
        "label": label,
        "score": 1.0 if label == "correct" else 0.0,
        "reason": reason,
        "judge_model": model,
        "usage": {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        },
    }
    write_json(layout.judge(item_id, cond), record)
    return record


def cmd_judge(args) -> int:
    layout = Layout(args.work)
    plan = read_json(layout.plan)
    todo = [
        (item["id"], cond)
        for item in plan["items"]
        for cond in plan["conditions"]
        if layout.answer(item["id"], cond).exists() and not layout.judge(item["id"], cond).exists()
    ]
    if not todo:
        print("没有待评分的回复")
        return 0
    client, model = _judge_client()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(judge_one, client, model, layout, i, c): (i, c) for i, c in todo}
        for future in as_completed(futures):
            item_id, cond = futures[future]
            try:
                record = future.result()
                print(f"{item_id}/{cond}：{record['label']}")
            except Exception as exc:  # noqa: BLE001
                print(f"{item_id}/{cond} 评分失败：{exc}")
    return 0


def _cost(prompt_tokens: int, completion_tokens: int) -> float:
    return prompt_tokens / 1e6 * PRICE_PROMPT + completion_tokens / 1e6 * PRICE_COMPLETION


def _usage_tokens(usage: dict) -> tuple[int, int]:
    if "prompt_tokens" in usage:
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    p = sum(int(row.get("prompt_tokens") or 0) for row in usage.values())
    c = sum(int(row.get("completion_tokens") or 0) for row in usage.values())
    return p, c


def cmd_report(args) -> int:
    layout = Layout(args.work)
    plan = read_json(layout.plan)
    conds = plan["conditions"]
    scores: dict[str, dict[str, float]] = defaultdict(dict)
    by_type: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    seconds: dict[str, list[float]] = defaultdict(list)
    prompt_chars: dict[str, list[int]] = defaultdict(list)
    tokens = defaultdict(lambda: [0, 0])
    experience_chars = []
    for item in plan["items"]:
        meta_path = layout.item_meta(item["id"])
        if meta_path.exists():
            meta = read_json(meta_path)
            experience_chars.append(len(meta.get("experience", "")))
            p, c = _usage_tokens(meta.get("usage", {}))
            tokens["item"][0] += p
            tokens["item"][1] += c
        for cond in conds:
            if layout.answer(item["id"], cond).exists():
                answer = read_json(layout.answer(item["id"], cond))
                seconds[cond].append(answer.get("seconds", 0))
                prompt_chars[cond].append(answer.get("prompt_chars", 0))
                p, c = _usage_tokens(answer.get("usage", {}))
                tokens["ask"][0] += p
                tokens["ask"][1] += c
            if layout.judge(item["id"], cond).exists():
                record = read_json(layout.judge(item["id"], cond))
                scores[cond][item["id"]] = record["score"]
                by_type[cond][item["relation_type"]].append(record["score"])
                p, c = _usage_tokens(record.get("usage", {}))
                tokens["judge"][0] += p
                tokens["judge"][1] += c
    for conv in sorted({item["conv"] for item in plan["items"]}):
        meta_path = layout.base_meta(conv)
        if meta_path.exists():
            for run in read_json(meta_path).get("runs", []):
                p, c = _usage_tokens(run.get("usage", {}))
                tokens["base"][0] += p
                tokens["base"][1] += c

    def avg(values):
        return round(sum(values) / len(values), 4) if values else None

    summary = {"conditions": {}, "paired": {}, "cost_cny": {}, "tokens": dict(tokens)}
    for cond in conds:
        summary["conditions"][cond] = {
            "judged": len(scores[cond]),
            "accuracy": avg(list(scores[cond].values())),
            "by_type": {t: avg(by_type[cond][t]) for t in RELATION_TYPES},
            "avg_seconds": avg(seconds[cond]),
            "avg_prompt_chars": avg(prompt_chars[cond]),
        }
    if "full" in conds:
        for other in conds:
            if other == "full":
                continue
            common = set(scores["full"]) & set(scores[other])
            summary["paired"][f"full_vs_{other}"] = {
                "pairs": len(common),
                "only_full_correct": sum(1 for i in common if scores["full"][i] > scores[other][i]),
                "only_other_correct": sum(1 for i in common if scores["full"][i] < scores[other][i]),
            }
    for stage, (p, c) in tokens.items():
        summary["cost_cny"][stage] = round(_cost(p, c), 2)
    summary["cost_cny"]["total"] = round(sum(summary["cost_cny"].values()), 2)
    summary["avg_experience_chars"] = avg(experience_chars)
    write_json(args.work / "report.json", summary)

    print(f"题数 {len(plan['items'])}；平均相处经验 {summary['avg_experience_chars']} 字")
    print(f"{'条件':<16}{'已评':>6}{'得分':>8}  " + "".join(f"{t:>8}" for t in RELATION_TYPES) + f"{'秒/问':>8}")
    for cond, row in summary["conditions"].items():
        acc = "-" if row["accuracy"] is None else f"{row['accuracy']:.3f}"
        types = "".join(
            f"{'-' if row['by_type'][t] is None else format(row['by_type'][t], '.3f'):>8}"
            for t in RELATION_TYPES
        )
        secs = "-" if row["avg_seconds"] is None else f"{row['avg_seconds']:.1f}"
        print(f"{cond:<16}{row['judged']:>6}{acc:>8}  {types}{secs:>8}")
    for name, row in summary["paired"].items():
        print(f"{name}：共 {row['pairs']} 对，仅 full 对 {row['only_full_correct']}，仅另一方对 {row['only_other_correct']}")
    print(f"估算费用（元）：{summary['cost_cny']}")
    return 0


# --------------------------------------------------------------------------- #
# 编排：子进程并行，已完成的跳过
# --------------------------------------------------------------------------- #


def _child(args, *extra: str, log_name: str) -> tuple[str, int]:
    layout = Layout(args.work)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--work", str(args.work), *extra]
    log = layout.logs() / f"{log_name}.log"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n=== {datetime.now().isoformat()} {' '.join(extra)}\n")
        handle.flush()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        code = subprocess.call(cmd, stdout=handle, stderr=subprocess.STDOUT, env=env, cwd=str(ROOT))
    return log_name, code


def _parallel(args, jobs: list[tuple[tuple[str, ...], str]], label: str) -> list[str]:
    failed = []
    if not jobs:
        return failed
    print(f"[{label}] {len(jobs)} 项，并行 {args.workers}", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(_child, args, *extra, log_name=name) for extra, name in jobs]
        for future in as_completed(futures):
            name, code = future.result()
            print(f"  {name}：{'完成' if code == 0 else f'失败（退出码 {code}，见 logs/{name}.log）'}", flush=True)
            if code != 0:
                failed.append(name)
    return failed


def cmd_run(args) -> int:
    layout = Layout(args.work)
    if not layout.plan.exists():
        cmd_plan(args)
    plan = read_json(layout.plan)
    items = plan["items"]
    conds = plan["conditions"]

    convs = sorted({item["conv"] for item in items})
    _parallel(args, [(("base", "--conv", str(c)), f"base-conv{c:02d}") for c in convs], "共用前缀")
    item_jobs = [
        (("item", "--item", item["id"]), f"item-{item['id']}")
        for item in items
        if not layout.item_meta(item["id"]).exists()
        and (layout.snapshot(item["conv"], item["cue_after_sessions"]) / "done").exists()
    ]
    _parallel(args, item_jobs, "逐题回放")
    ask_jobs = [
        (("ask", "--item", item["id"], "--cond", cond), f"ask-{item['id']}-{cond}")
        for item in items
        for cond in conds
        if layout.item_meta(item["id"]).exists() and not layout.answer(item["id"], cond).exists()
    ]
    _parallel(args, ask_jobs, "提问")
    cmd_judge(args)
    return cmd_report(args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LoCoMo-Plus 认知记忆测评（匠石）")
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK, help="测评工作目录")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_plan_options(p):
        p.add_argument("--data", type=Path, default=DEFAULT_DATA, help="Locomo-Plus/data 目录")
        p.add_argument("--per-type", type=int, default=5, help="每种关系抽几条；0 表示全部")
        p.add_argument("--seed", type=int, default=20260930)
        p.add_argument("--items", default="", help="直接指定题号，逗号分隔，如 0,17,p203")
        p.add_argument(
            "--conditions",
            type=lambda s: tuple(x for x in s.split(",") if x),
            default=DEFAULT_CONDITIONS,
            help=f"逗号分隔，可选 {','.join(CONDITIONS)}",
        )

    add_plan_options(sub.add_parser("plan", help="选题"))
    p = sub.add_parser("base", help="回放共用前缀并存快照")
    p.add_argument("--conv", type=int, required=True)
    p = sub.add_parser("item", help="逐题回放并整理人物经验")
    p.add_argument("--item", required=True)
    p = sub.add_parser("ask", help="在某条件下提问")
    p.add_argument("--item", required=True)
    p.add_argument("--cond", choices=tuple(CONDITIONS), required=True)
    p.add_argument("--keep", action="store_true", help="保留本次提问的数据目录")
    p = sub.add_parser("judge", help="模型裁判评分")
    p.add_argument("--workers", type=int, default=4)
    sub.add_parser("report", help="汇总")
    p = sub.add_parser("run", help="全流程")
    add_plan_options(p)
    p.add_argument("--workers", type=int, default=3, help="并行子进程数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.work = args.work.resolve()
    for cond in getattr(args, "conditions", ()):
        if cond not in CONDITIONS:
            raise SystemExit(f"未知条件：{cond}")
    handlers = {
        "plan": cmd_plan,
        "base": cmd_base,
        "item": cmd_item,
        "ask": cmd_ask,
        "judge": cmd_judge,
        "report": cmd_report,
        "run": cmd_run,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
