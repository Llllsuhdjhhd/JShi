"""Manual scenario library CLI. Run --help for commands."""
import argparse
import asyncio
import importlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from jshi.testing.entry_cases.store import CaseStore
from jshi.testing.entry_cases.runner import replay


def factory(spec):
    if not spec:
        return None
    module, name = spec.split(':', 1)
    return getattr(importlib.import_module(module), name)


def main():
    p = argparse.ArgumentParser(description='连续录音与冻结JEV场景回放；真实依赖必须明确配置')
    p.add_argument('--root', type=Path, default=Path('.jshi/entry_test_assets'))
    sub = p.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('import-wav'); imp.add_argument('file', type=Path)
    add = sub.add_parser('save'); add.add_argument('file', type=Path)
    sub.add_parser('list')
    run = sub.add_parser('run'); run.add_argument('id')
    run.add_argument('--decoder-factory', help='module:function(state_dir) -> decoder')
    run.add_argument('--gate-factory', help='module:function(mode,state_dir) -> VoiceJEV; decision bridge must be supplied')
    run.add_argument('--runs', type=Path, default=Path('test_runs/entry'))
    run.add_argument('--fast', action='store_true', help='只适用于内容回归，不评价时序')
    args = p.parse_args(); store = CaseStore(args.root)
    if args.command == 'import-wav':
        print(store.import_wav(args.file.read_bytes(), source={'kind': 'file', 'name': args.file.name}))
    elif args.command == 'save':
        print(store.save(json.loads(args.file.read_text(encoding='utf-8')))['id'])
    elif args.command == 'list':
        print(json.dumps(store.list(), ensure_ascii=False, indent=2))
    else:
        print(asyncio.run(replay(store, store.load(args.id), args.runs,
            decoder_factory=factory(args.decoder_factory), gate_factory=factory(args.gate_factory), realtime=not args.fast)))


if __name__ == '__main__':
    main()
