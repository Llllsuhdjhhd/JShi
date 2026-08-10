from __future__ import annotations

import argparse
import os
from pathlib import Path

from jshi.identity import IdentityProfile, IdentityRepository
from jshi.models import EchoModel, ModelPort, OpenAICompatibleModel
from jshi.recognition import ObjectProfile, new_object_id
from jshi.subject import (
    EpistemicStatus,
    HistoryKind,
    PersonalKind,
    PersonalStatus,
    SubjectProcess,
    SubjectRepository,
)


def _model_from_environment() -> ModelPort:
    endpoint = os.getenv("JSHI_MODEL_ENDPOINT")
    api_key = os.getenv("JSHI_MODEL_API_KEY")
    model = os.getenv("JSHI_MODEL_NAME")
    if endpoint and api_key and model:
        return OpenAICompatibleModel(endpoint, api_key, model)
    return EchoModel()


def _runtime(
    data_dir: Path,
) -> tuple[SubjectProcess, IdentityRepository, SubjectRepository]:
    identities = IdentityRepository(data_dir / "identities.json")
    subjects = SubjectRepository(data_dir / "subject.sqlite3")
    process = SubjectProcess(subjects, identities, _model_from_environment())
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
        "experience", aliases=["chat"], help="进行一次外部活动"
    )
    experience.add_argument("subject_id")
    experience.add_argument("text")
    experience.add_argument(
        "--speaker",
        default=None,
        help="说话人名字或对象标识（渠道未提供对象时必填）",
    )
    experience.add_argument("--channel", default=None, help="渠道标识（可选）")

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

    propose = commands.add_parser(
        "propose-open",
        help="在认知之后记录主体面未完成现实（必须带来源认知或事实标识）",
    )
    propose.add_argument("subject_id")
    propose.add_argument("content")
    propose.add_argument(
        "--source",
        action="append",
        dest="sources",
        required=True,
        help="来源标识，可重复；通常为认知内容 id",
    )

    close = commands.add_parser(
        "close-personal", help="结束未完成现实（主体面）、承诺等个人内容"
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

    add_object = commands.add_parser("add-object", help="预注册对话对象档案")
    add_object.add_argument("label", help="显示名")
    add_object.add_argument("--aliases", default="", help="别名，逗号分隔")
    add_object.add_argument("--channel", default=None, help="渠道标识（可选）")
    add_object.add_argument("--source", default="cli-import", help="导入来源")
    return parser


def main() -> None:
    args = _parser().parse_args()
    process, identities, subjects = _runtime(args.data_dir)

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
    elif args.command in {"experience", "chat"}:
        try:
            result = process.experience(
                args.subject_id,
                args.text,
                object_ref=args.speaker,
                channel=args.channel,
            )
        except ValueError as exc:
            print(f"错误：{exc}")
            return
        print(result.action_text)
        print(f"[活动 {result.activity.id}；认知 {result.thought.id}]")
    elif args.command in {"reflect", "inner"}:
        result = process.reflect(args.subject_id, args.prompt)
        print(result.content)
        print(f"[认知 {result.id}；状态 {result.epistemic_status.value}]")
    elif args.command == "add-personal":
        item = process.add_personal_item(
            args.subject_id,
            PersonalKind(args.kind),
            args.content,
            importance=args.importance,
        )
        print(f"已添加：{item.id}")
    elif args.command == "propose-open":
        item = process.propose_open_matter(
            args.subject_id,
            args.content,
            source_ids=tuple(args.sources),
        )
        print(f"已记录未完成现实（主体面）：{item.id}")
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
            preview = process.preview_state(
                args.subject_id,
                args.text,
                object_ref=args.speaker,
                channel=args.channel,
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
        print(
            f"归属：{list(preview.attribution.concern_ids)}"
            f"（{preview.attribution.status}，依据 {list(preview.attribution.basis)}）"
        )
        print(f"组装模式：{preview.mode}")
        print(f"价值：{list(assembled.subject_state.salient_values)}")
        print(f"承诺：{list(assembled.subject_state.commitments)}")
        print(f"未完成现实（主体面）：{list(assembled.subject_state.concerns)}")
        print(f"开放事项 id：{list(assembled.open_matter_ids)}")
        print("召回：")
        for item in assembled.recalled:
            print(f"  - {item.event_type}: {item.text}")
    elif args.command == "add-object":
        aliases = tuple(
            alias.strip() for alias in args.aliases.split(",") if alias.strip()
        )
        profile = process.profiles.create(
            ObjectProfile(
                object_id=new_object_id(),
                label=args.label,
                aliases=aliases,
                channel=args.channel,
                source=args.source,
                status="confirmed",
            )
        )
        print(f"已注册对象：{profile.object_id}（{profile.label}）")


if __name__ == "__main__":
    main()
