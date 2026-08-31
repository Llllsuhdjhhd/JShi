from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from jshi.experienceledger import SqliteExperienceLedger
from jshi.identity import IdentityProfile, IdentityRepository
from jshi.memory import MemoryShell, build_memory_backend
from jshi.models import EchoModel, ModelPort, OpenAICompatibleModel
from jshi.privilege import PromptRuleStore, SuperPermissionStore
from jshi.recognition import CarrierEntry, ObjectProfile, new_object_id
from jshi.skill import CognitionSkill, SkillModelPort
from jshi.subject import (
    EpistemicStatus,
    HistoryKind,
    PersonalKind,
    PersonalStatus,
    SubjectProcess,
    SubjectRepository,
)


def parse_env_text(text: str) -> dict[str, str]:
    """解析 KEY=VALUE（# 注释；已设置的环境变量不在这里覆盖）。"""
    values: dict[str, str] = {}
    for raw in text.splitlines():
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
        values[key.strip()] = value
    return values


def _load_local_env() -> None:
    """从仓库根或当前目录的 .env 填入尚未设置的环境变量。测试中不读文件。"""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    seen: set[Path] = set()
    explicit = os.getenv("JSHI_ENV_FILE")
    if explicit:
        candidates = (Path(explicit),)
    else:
        repo = Path(__file__).resolve().parents[3]
        candidates = [Path.cwd() / ".env"]
        if (repo / "pyproject.toml").exists():
            candidates.append(repo / ".env")
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        parsed = parse_env_text(path.read_text(encoding="utf-8"))
        for key, value in parsed.items():
            os.environ.setdefault(key, value)


def _model_from_environment() -> ModelPort:
    endpoint = os.getenv("JSHI_MODEL_ENDPOINT")
    api_key = os.getenv("JSHI_MODEL_API_KEY")
    model = os.getenv("JSHI_MODEL_NAME")
    if endpoint and api_key and model:
        base = OpenAICompatibleModel(endpoint, api_key, model)
    else:
        base = EchoModel()
    # 05 认知走结构化 skill（JSON Schema + 解析 + 降级）；reflection 等内部活动走裸模型。
    return SkillModelPort(CognitionSkill(base))


def _runtime(
    data_dir: Path,
) -> tuple[SubjectProcess, IdentityRepository, SubjectRepository]:
    identities = IdentityRepository(data_dir / "identities.json")
    subjects = SubjectRepository(data_dir / "subject.sqlite3")
    backend = build_memory_backend(subjects, data_dir)
    prompt_rules = PromptRuleStore(data_dir / "prompt_rules.json")
    if type(backend).__name__ != "InProcessMemoryBackend":
        print(f"[jshi] memory backend: {type(backend).__name__}", file=sys.stderr)
    process = SubjectProcess(
        subjects,
        identities,
        _model_from_environment(),
        memory=MemoryShell(backend),
        activity_ledger=SqliteExperienceLedger(data_dir / "subject.sqlite3"),
        prompt_rules=prompt_rules,
    )
    return process, identities, subjects


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="匠石主体过程实验框架")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.getenv("JSHI_DATA_DIR", ".jshi")),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="创建一个基础匠石")
    create.add_argument("subject_id")
    create.add_argument("--name", default="匠石")
    create.add_argument("--origin", default="基础型匠石")

    experience = commands.add_parser(
        "experience", help="进行一次外部活动（单句；日常对话请用 talk）"
    )
    experience.add_argument("subject_id")
    experience.add_argument("text")
    experience.add_argument(
        "--speaker",
        default=None,
        help="说话人名字或对象标识（渠道未提供对象时必填）",
    )
    experience.add_argument("--channel", default=None, help="渠道标识（可选）")
    experience.add_argument(
        "--carrier",
        action="append",
        default=[],
        help="识别载体，格式 kind:value，可重复（如 voiceprint:vp-1）",
    )
    experience.add_argument(
        "--object",
        action="append",
        default=[],
        help="对象映射表条目，格式 名字:object_id，可重复（如 宝玉:OBJ-BAO）",
    )

    talk = commands.add_parser(
        "talk",
        aliases=["chat"],
        help="对话：启动后直接打字；/speaker 长期记住对象",
    )
    talk.add_argument(
        "subject_id",
        nargs="?",
        default=None,
        help="主体 id；省略则用上次会话",
    )
    talk.add_argument(
        "--speaker",
        default=None,
        help="说话人；省略则用上次 /speaker 记下的对象",
    )
    talk.add_argument("--channel", default=None, help="渠道标识（可选）")
    talk.add_argument(
        "--carrier",
        action="append",
        default=[],
        help="识别载体，格式 kind:value，可重复",
    )
    talk.add_argument(
        "--tui",
        action="store_true",
        help="套一层全屏界面（需 pip install textual）；默认仍是原来的一行输入",
    )
    talk.add_argument(
        "--plain",
        action="store_true",
        help="原来的一行输入（默认；与 --tui 同时出现时以本项为准）",
    )

    reflect = commands.add_parser(
        "reflect", aliases=["inner"], help="进行一次内部反思"
    )
    reflect.add_argument("subject_id")
    reflect.add_argument("prompt")

    add = commands.add_parser("add-personal", help="添加个人内容")
    add.add_argument("subject_id")
    add.add_argument("kind", choices=[kind.value for kind in PersonalKind])
    add.add_argument("content")
    add.add_argument(
        "--importance",
        type=float,
        default=1.0,
        help="重要程度（装载时按此排序、预算内截断）",
    )

    import_values = commands.add_parser(
        "import-values", help="按 100 JSON 导入价值观/边界"
    )
    import_values.add_argument("subject_id")
    import_values.add_argument("path", type=Path)

    export_values = commands.add_parser(
        "export-values", help="导出 100 JSON 文档"
    )
    export_values.add_argument("subject_id")
    export_values.add_argument("--out", type=Path, default=None)

    propose_value = commands.add_parser(
        "propose-value", help="100：提出候选价值或边界"
    )
    propose_value.add_argument("subject_id")
    propose_value.add_argument("content")
    propose_value.add_argument(
        "--role", choices=["value", "boundary"], default="value"
    )
    propose_value.add_argument("--summary", default="")
    propose_value.add_argument("--stance", default="")
    propose_value.add_argument(
        "--source-type",
        default="human_intervention",
        help="classic_work / classic_novel / model_proposal / "
        "human_intervention / derived_experience / governance_review",
    )
    propose_value.add_argument("--source-id", default=None)
    propose_value.add_argument("--importance", type=float, default=1.0)
    propose_value.add_argument(
        "--binding",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="仅边界：是否强制（缺省为强制）",
    )

    review_value = commands.add_parser(
        "review-value", help="100：审核候选 accept / reject"
    )
    review_value.add_argument("item_id")
    review_value.add_argument("decision", choices=["accept", "reject"])
    review_value.add_argument("reason")
    review_value.add_argument("--reviewer", default="cli")

    lock_value = commands.add_parser("lock-value", help="100：加锁已接受条目")
    lock_value.add_argument("item_id")
    lock_value.add_argument("reason")
    lock_value.add_argument("--approver", default="cli")

    supersede_value = commands.add_parser(
        "supersede-value", help="100：用新条目废止旧条目"
    )
    supersede_value.add_argument("item_id")
    supersede_value.add_argument("replacement_id")
    supersede_value.add_argument("reason")

    list_values = commands.add_parser("values", help="列出 100 价值库")
    list_values.add_argument("subject_id")

    close = commands.add_parser(
        "close-personal", help="结束承诺等个人内容"
    )
    close.add_argument("item_id")
    close.add_argument(
        "status",
        choices=[
            PersonalStatus.COMPLETED.value,
            PersonalStatus.RELEASED.value,
            PersonalStatus.SUPERSEDED.value,
        ],
    )
    close.add_argument("reason")

    transition = commands.add_parser("transition", help="改变一项认知的认识状态")
    transition.add_argument("content_id")
    transition.add_argument(
        "status",
        choices=[
            EpistemicStatus.CONSIDERING.value,
            EpistemicStatus.PROVISIONAL.value,
            EpistemicStatus.ACCEPTED.value,
            EpistemicStatus.REJECTED.value,
            EpistemicStatus.SUSPENDED.value,
            EpistemicStatus.REVISED.value,
        ],
    )
    transition.add_argument("reason")

    personal = commands.add_parser("personal", help="查看个人世界")
    personal.add_argument("subject_id")
    personal.add_argument(
        "--kind", choices=[kind.value for kind in PersonalKind], default=None
    )
    personal.add_argument("--all", action="store_true")

    history = commands.add_parser("history", help="查看事实或主体历史")
    history.add_argument("subject_id")
    history.add_argument(
        "--kind", choices=[kind.value for kind in HistoryKind], default=None
    )
    history.add_argument("--limit", type=int, default=20)

    recall = commands.add_parser("recall", help="查询当前记忆端口的相关片段")
    recall.add_argument("subject_id")
    recall.add_argument("query")
    recall.add_argument("--limit", type=int, default=None)
    recall.add_argument("--object-id", default=None, help="按对话对象过滤记忆")
    recall.add_argument(
        "--level",
        type=int,
        default=1,
        help="回忆档位 1–9；未指定 --limit 时决定默认返回条数",
    )

    state = commands.add_parser("state", help="查看身份和活跃个人内容")
    state.add_argument("subject_id")

    preview = commands.add_parser(
        "preview-state", help="预览进模型前的当前状态组装（不调用模型）"
    )
    preview.add_argument("subject_id")
    preview.add_argument("text")
    preview.add_argument(
        "--speaker",
        default=None,
        help="说话人名字或对象标识（渠道未提供对象时必填）",
    )
    preview.add_argument("--channel", default=None, help="渠道标识（可选）")
    preview.add_argument(
        "--carrier",
        action="append",
        default=[],
        help="识别载体，格式 kind:value，可重复（如 voiceprint:vp-1）",
    )

    add_object = commands.add_parser("add-object", help="预注册对话对象档案")
    add_object.add_argument("label", help="显示名")
    add_object.add_argument("--aliases", default="", help="别名，逗号分隔")
    add_object.add_argument("--channel", default=None, help="渠道标识（可选）")
    add_object.add_argument("--source", default="cli-import", help="导入来源")
    add_object.add_argument(
        "--carrier",
        action="append",
        default=[],
        help="识别载体，格式 kind:value，可重复（如 voiceprint:vp-1）",
    )

    grant_super = commands.add_parser(
        "grant-super", help="授予某对象超级权限并设置登录密码"
    )
    grant_super.add_argument("object", help="对象名或 object_id")
    grant_super.add_argument("--password", required=True, help="登录密码")
    return parser


def main() -> None:
    _load_local_env()
    args = _parser().parse_args()
    process, identities, subjects = _runtime(args.data_dir)
    super_permissions = SuperPermissionStore(args.data_dir / "super_permissions.json")

    if args.command == "create":
        identities.create(
            IdentityProfile(
                subject_id=args.subject_id,
                name=args.name,
                origin=args.origin,
                narrative=f"我是{args.name}，从人类文明的共同基础出发继续成长。",
            )
        )
        print(f"已创建：{args.subject_id}")
    elif args.command == "experience":
        try:
            carriers = tuple(_parse_carrier(item) for item in args.carrier)
            objects = _parse_objects(args.object)
            result = process.experience(
                args.subject_id,
                args.text,
                object_ref=args.speaker,
                channel=args.channel,
                carriers=carriers,
                objects=objects,
            )
        except ValueError as exc:
            print(f"错误：{exc}")
            return
        print(result.action_text)
        print(f"[活动 {result.activity.id}；mode {result.response_plan.mode}]")
    elif args.command in {"talk", "chat"}:
        _run_talk(process, identities, args, super_permissions)
    elif args.command in {"reflect", "inner"}:
        result = process.reflect(args.subject_id, args.prompt)
        print(result.content)
        print(f"[认知 {result.id}；状态 {result.epistemic_status.value}]")
    elif args.command == "add-personal":
        try:
            item = process.add_personal_item(
                args.subject_id,
                PersonalKind(args.kind),
                args.content,
                importance=args.importance,
            )
        except ValueError as exc:
            print(f"错误：{exc}")
            return
        print(f"已添加：{item.id}")
    elif args.command == "import-values":
        _run_import_values(process, args.subject_id, args.path)
    elif args.command == "export-values":
        _run_export_values(process, args.subject_id, args.out)
    elif args.command == "propose-value":
        _run_propose_value(process, args)
    elif args.command == "review-value":
        _run_review_value(process, args)
    elif args.command == "lock-value":
        _run_lock_value(process, args)
    elif args.command == "supersede-value":
        _run_supersede_value(process, args)
    elif args.command == "values":
        _run_list_values(process, args.subject_id)
    elif args.command == "close-personal":
        item = process.close_personal_item(
            args.item_id, PersonalStatus(args.status), args.reason
        )
        print(f"已更新：{item.id} -> {item.status.value}")
    elif args.command == "transition":
        content = process.transition_cognition(
            args.content_id, EpistemicStatus(args.status), args.reason
        )
        print(f"已更新：{content.id} -> {content.epistemic_status.value}")
    elif args.command == "personal":
        kind = PersonalKind(args.kind) if args.kind else None
        for item in subjects.list_personal_items(
            args.subject_id, kind=kind, active_only=not args.all
        ):
            print(
                f"{item.id} {item.kind.value}/{item.status.value} "
                f"r{item.revision} {item.content}"
            )
    elif args.command == "history":
        kind = HistoryKind(args.kind) if args.kind else None
        for record in subjects.list_history(args.subject_id, kind, args.limit):
            print(
                f"{record.created_at.isoformat()} "
                f"{record.kind.value}/{record.event_type} {dict(record.content)}"
            )
    elif args.command == "recall":
        fragments = process.memory.recall(
            args.subject_id,
            args.query,
            limit=args.limit,
            object_id=args.object_id,
            level=args.level,
        )
        for fragment in fragments:
            print(
                f"{fragment.event_id} {fragment.kind}/{fragment.event_type} "
                f"{fragment.text}"
            )
    elif args.command == "state":
        profile = identities.get(args.subject_id)
        items = subjects.list_personal_items(args.subject_id)
        print(
            f"{profile.name} ({profile.subject_id})\n"
            f"来源：{profile.origin}\n"
            f"叙事：{profile.narrative}\n"
            f"活跃个人内容：{len(items)}"
        )
    elif args.command == "preview-state":
        try:
            carriers = tuple(_parse_carrier(item) for item in args.carrier)
            preview = process.preview_state(
                args.subject_id,
                args.text,
                object_ref=args.speaker,
                channel=args.channel,
                carriers=carriers,
            )
        except ValueError as exc:
            print(f"错误：{exc}")
            return
        assembled = preview.assembled
        print(f"输入：{assembled.input_text}")
        print(
            f"说话人：{preview.speaker.label}（对象 {preview.speaker.object_id}，"
            f"置信度 {preview.speaker.confidence}，状态 {preview.speaker.status}）"
        )
        print(f"上下文视图 v{preview.context_view.version}：")
        if preview.context_view.context_text:
            print(preview.context_view.context_text)
        else:
            print("  （空）")
        print(f"价值：{list(assembled.subject_state.salient_values)}")
        print(f"承诺：{list(assembled.subject_state.commitments)}")
        print(f"既往段引用：{list(assembled.context_view.segment_refs)}")
        print("装载报告：")
        for report in assembled.source_report:
            line = f"  - {report.source}: {len(report.loaded_ids)} 条（{report.status}）"
            if report.skipped_ids:
                line += f"，跳过 {len(report.skipped_ids)}"
            if report.error:
                line += f"，错误：{report.error}"
            print(line)
        if assembled.recalled:
            print("追加召回：")
            for item in assembled.recalled:
                print(f"  - {item.event_type}: {item.text}")
        else:
            print("追加召回：无")
    elif args.command == "add-object":
        aliases = tuple(
            alias.strip() for alias in args.aliases.split(",") if alias.strip()
        )
        carriers = tuple(_parse_carrier(item) for item in args.carrier)
        profile = process.profiles.create(
            ObjectProfile(
                object_id=new_object_id(),
                label=args.label,
                aliases=aliases,
                carriers=carriers,
                channel=args.channel,
                source=args.source,
                status="confirmed",
            )
        )
        print(f"已注册对象：{profile.object_id}（{profile.label}）")
    elif args.command == "grant-super":
        _run_grant_super(process, super_permissions, args)


def _require_values(process):
    values = getattr(process, "values", None)
    if values is None:
        raise ValueError("当前个人世界没有 100 接口")
    return values


def _run_import_values(process, subject_id: str, path: Path) -> None:
    try:
        values = _require_values(process)
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}")
        return
    entries = document.get("entries")
    if not isinstance(entries, list):
        print("错误：JSON 须含 entries 数组，见 doc/examples/values-import.example.json")
        return
    report = values.import_entries(subject_id, entries)
    print(
        f"已导入 {len(report.imported_ids)} 条，跳过 {len(report.skipped_ids)} 条"
    )
    for item_id in report.imported_ids:
        print(f"  + {item_id}")
    for item_id in report.skipped_ids:
        print(f"  skip {item_id}")


def _run_export_values(process, subject_id: str, out: Path | None) -> None:
    try:
        values = _require_values(process)
        document = values.export_document(subject_id)
    except ValueError as exc:
        print(f"错误：{exc}")
        return
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if out is None:
        print(text, end="")
        return
    out.write_text(text, encoding="utf-8")
    print(f"已写出 {out}（{len(document.get('entries', []))} 条）")


def _run_propose_value(process, args) -> None:
    from jshi.personalworld import ValueSource

    try:
        values = _require_values(process)
        item = values.propose_value(
            args.subject_id,
            args.content,
            role=args.role,
            summary=args.summary,
            stance=args.stance,
            source_type=ValueSource(args.source_type),
            source_id=args.source_id,
            importance=args.importance,
            binding=args.binding,
        )
    except ValueError as exc:
        print(f"错误：{exc}")
        return
    print(f"已提出候选：{item.id} ({item.status.value})")


def _run_review_value(process, args) -> None:
    try:
        values = _require_values(process)
        item = values.review_value(
            args.item_id, args.decision, args.reason, args.reviewer
        )
    except (KeyError, ValueError) as exc:
        print(f"错误：{exc}")
        return
    print(f"已审核：{item.id} -> {item.status.value}")


def _run_lock_value(process, args) -> None:
    try:
        values = _require_values(process)
        item = values.lock_value(args.item_id, args.reason, args.approver)
    except (KeyError, ValueError) as exc:
        print(f"错误：{exc}")
        return
    print(f"已加锁：{item.id} -> {item.status.value}")


def _run_supersede_value(process, args) -> None:
    try:
        values = _require_values(process)
        item = values.supersede_value(
            args.item_id, args.replacement_id, args.reason
        )
    except (KeyError, ValueError) as exc:
        print(f"错误：{exc}")
        return
    print(f"已废止：{item.id} -> {item.status.value}")


def _run_list_values(process, subject_id: str) -> None:
    try:
        values = _require_values(process)
        document = values.export_document(subject_id)
    except ValueError as exc:
        print(f"错误：{exc}")
        return
    entries = document.get("entries") or []
    if not entries:
        print("（空）")
        return
    for entry in entries:
        print(
            f"{entry.get('id')} {entry.get('role')}/{entry.get('status')} "
            f"{entry.get('summary') or entry.get('content')}"
        )


def _resolve_object(process, ref: str):
    profile = process.profiles.get(ref)
    if profile is not None:
        return profile
    matches = process.profiles.find_by_names(ref)
    if len(matches) == 1:
        return matches[0]
    return None


def _run_grant_super(process, store, args) -> None:
    profile = _resolve_object(process, args.object)
    if profile is None:
        print(f"错误：找不到对象 {args.object}（请先 add-object 注册）")
        return
    try:
        store.grant(profile.object_id, profile.label, args.password)
    except ValueError as exc:
        print(f"错误：{exc}")
        return
    print(f"已授予超级权限：{profile.label}（{profile.object_id}）")


def _parse_carrier(raw: str) -> CarrierEntry:
    kind, _, value = raw.partition(":")
    if not kind.strip() or not value.strip():
        raise ValueError(f"invalid carrier format: {raw!r} (expected kind:value)")
    return CarrierEntry(kind=kind.strip(), value=value.strip())


def _parse_objects(raw: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in raw:
        name, _, object_id = item.partition(":")
        if not name.strip() or not object_id.strip():
            raise ValueError(f"invalid object mapping: {item!r} (expected 名字:object_id)")
        mapping[name.strip()] = object_id.strip()
    return mapping


def _want_tui(args) -> bool:
    if getattr(args, "plain", False):
        return False
    return bool(getattr(args, "tui", False))


def _textual_installed() -> bool:
    return importlib.util.find_spec("textual") is not None


def _run_talk(process, identities, args, super_permissions=None) -> None:
    """启动对话。默认原来的一行输入；--tui 才套全屏壳。"""
    from jshi.app.talk_plain import run_plain
    from jshi.app.talk_session import TalkSetupError, prepare_talk

    try:
        carriers = tuple(_parse_carrier(item) for item in args.carrier)
        session = prepare_talk(
            process,
            identities,
            args.data_dir,
            args.subject_id,
            args.speaker,
            args.channel,
            carriers,
            super_permissions,
        )
    except (ValueError, TalkSetupError) as exc:
        print(exc if str(exc).startswith("错误") else f"错误：{exc}")
        return
    if not _want_tui(args):
        run_plain(session)
        return
    try:
        interactive = sys.stdin.isatty()
    except Exception:
        interactive = False
    if not interactive:
        print("非交互终端，使用原来的一行输入。", file=sys.stderr)
        run_plain(session)
        return
    if not _textual_installed():
        print(
            "未安装 textual，使用原来的一行输入。pip install textual",
            file=sys.stderr,
        )
        run_plain(session)
        return
    from jshi.app.talk_tui import run_tui

    run_tui(session)


if __name__ == "__main__":
    main()
