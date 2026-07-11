from __future__ import annotations

import argparse
import os
from pathlib import Path

from jshi.chance import NoChancePolicy
from jshi.governance import GovernanceService
from jshi.identity import (
    IdentityProfile,
    IdentityRepository,
    SimpleSelfPort,
)
from jshi.infrastructure import PluginRegistry, SQLiteEventStore
from jshi.inner import InnerActivity, InnerActivityKind
from jshi.mind import default_mind_plugins
from jshi.models import EchoModel, ModelPort, OpenAICompatibleModel
from jshi.orchestration import Orchestrator


def _model_from_environment() -> ModelPort:
    endpoint = os.getenv("JSHI_MODEL_ENDPOINT")
    api_key = os.getenv("JSHI_MODEL_API_KEY")
    model = os.getenv("JSHI_MODEL_NAME")
    if endpoint and api_key and model:
        return OpenAICompatibleModel(endpoint, api_key, model)
    return EchoModel()


def _runtime(data_dir: Path) -> tuple[Orchestrator, IdentityRepository, SQLiteEventStore]:
    identities = IdentityRepository(data_dir / "identities.json")
    events = SQLiteEventStore(data_dir / "events.sqlite3")
    orchestrator = Orchestrator(
        event_store=events,
        identities=identities,
        self_port=SimpleSelfPort(),
        plugins=PluginRegistry(default_mind_plugins()),
        model=_model_from_environment(),
        governance=GovernanceService(),
        chance=NoChancePolicy(),
    )
    return orchestrator, identities, events


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="匠石主体框架")
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

    chat = commands.add_parser("chat", help="进行一轮外部互动")
    chat.add_argument("subject_id")
    chat.add_argument("text")

    inner = commands.add_parser("inner", help="触发一次内在活动")
    inner.add_argument("subject_id")
    inner.add_argument("prompt")
    inner.add_argument(
        "--kind",
        choices=[kind.value for kind in InnerActivityKind],
        default=InnerActivityKind.REFLECTION.value,
    )
    inner.add_argument("--imagine", action="store_true")

    events = commands.add_parser("events", help="查看事件")
    events.add_argument("subject_id")
    events.add_argument("--limit", type=int, default=20)

    state = commands.add_parser("state", help="查看身份摘要")
    state.add_argument("subject_id")
    return parser


def main() -> None:
    args = _parser().parse_args()
    orchestrator, identities, events = _runtime(args.data_dir)

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
    elif args.command == "chat":
        result = orchestrator.interact(args.subject_id, args.text)
        print(result.output_event.content["text"])
    elif args.command == "inner":
        activity = InnerActivity(
            kind=InnerActivityKind(args.kind),
            prompt=args.prompt,
            allow_imagination=args.imagine,
        )
        result = orchestrator.run_inner(args.subject_id, activity)
        print(result.output_event.content["text"])
    elif args.command == "events":
        for event in events.list_for_subject(args.subject_id, args.limit):
            print(
                f"{event.created_at.isoformat()} "
                f"{event.kind.value}/{event.truth_status.value} {dict(event.content)}"
            )
    elif args.command == "state":
        profile = identities.get(args.subject_id)
        print(
            f"{profile.name} ({profile.subject_id})\n"
            f"来源：{profile.origin}\n"
            f"叙事：{profile.narrative}\n"
            f"修订：{profile.revision}"
        )


if __name__ == "__main__":
    main()
