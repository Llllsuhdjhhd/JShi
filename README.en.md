# JShi（匠石）

**An experimental “subject process” for a long-lived AI companion.**

[中文 README](README.md)

Most AI assistant frameworks ask how to finish a task. JShi asks something else: **how an AI can have a continuous self**—remember, recognize people, speak with proportion, and tell apart *what it knows* from *what it has said*.

The Chinese name 匠石 comes from Zhuangzi (*Xu Wugui*): a craftsman and his partner who, through long practice together, could do what no stranger could. The point is not only skill, but a relationship that cannot be substituted. This project leans that way: the companion becomes itself through shared history, not only a mirror that understands the user.

> Experimental. Interfaces will change. See **Status** below.

## What one turn looks like

```
object recognition → state assembly → open activity → cognition (response plan)
  → expression (speech / embodiment) → rewrite the field → close
```

Ways this differs from the usual chat stack:

- **The turn context is not chat history.** It is the result of *state assembly*: subject identity, the other person, the previous active field, personal world, memory cues, tool feedback.
- **Active field = the workbench now.** Cognition *rewrites the whole field* each turn under the current style pack. It is JShi’s present desk, not a transcript.
- **Field blocks come in kinds.** `(我知道)…` / “I know …” are facts currently held; `(未开口)…` / “unsaid …” are matters kept but not spoken. Neither is “what I already said aloud.”

## Core design

- **Subject boundary.** Capability code only proposes candidates and process feedback; it does not write the subject’s long-term state.
- **Tool use is bookkept separately.** Stage 05 only decides whether to use a tool and gives one `need` line. Stage 200 splits intake ledger / planning (205) / engine adapter (206) / hang list (210) / wrapping. Outside: three doors only (`intake` / `list_visible` / `cancel`).
- **Memory is a port.** Default in-process backend; production uses REMS3 (neighbor repo, distributed separately).
- **Persona is a pluggable style pack** (e.g. Wood / Susie / Smith).
- **Looking at itself.** Effectiveness analysis + stage 300 introspection (early).

## Quick start

Needs **Python ≥ 3.11**. Core uses the standard library only.

```bash
pip install -e .

python -m jshi.app.cli --data-dir .tmp/demo create stone
python -m jshi.app.cli --data-dir .tmp/demo experience stone "hello" \
    --speaker mars --object mars:OBJ-M
```

Multi-turn talk (persistent session):

```bash
python -m jshi.app.cli talk stone --speaker mars
```

### What to configure

It runs with nothing configured—but that is only a skeleton without intelligence. For a real run, set these (see `.env.example`):

| Want | Need | If missing |
|---|---|---|
| Real dialogue | Model trio: `JSHI_MODEL_ENDPOINT` / `JSHI_MODEL_API_KEY` / `JSHI_MODEL_NAME` | Falls back to stock lines; no crash |
| Real tools | Install Pi (`@earendil-works/pi-coding-agent`). Default tool engine | 205 returns an honest failure: cannot reach the tool engine |
| Cross-session memory | Neighbor repo REMS3 (not shipped here); set `JSHI_MEMORY_BACKEND` | In-process backend; still works |

**Tests need no env vars.** `pytest` uses a fake model port and temp dirs, offline:

```bash
pytest -q
```

## Layout

```
src/jshi/          main path and modules
  subject/         process orchestration (seven stages)
  core/ models/    state, prompts, model port
  skill/ style/    cognition skill, style packs, field
  tool/            200 tool use (intake / plan / engine / hang)
doc/               design docs (mostly Chinese)
  design/          00 overview, 03 assembly, 05 cognition, 09 memory, 200 tools…
方案(coding)/      implementation notes and revisions
tests/             unit tests; tests/live/ needs a real model or backend
```

## Where to read next

Design docs are primarily in Chinese; start here:

1. `doc/总体架构.md` — overall picture
2. `doc/design/00-流程总览与输入边界.md` — seven stages and input boundary
3. `doc/design/03-状态组装.md`, `doc/design/05-认知.md` — how turn context is built
4. `doc/design/工具使用.md` — 200 bookkeeping and feedback paths
5. `doc/概念词汇.md` — glossary (active field / field block / hang / intake…)
6. `doc/匠石项目构想.md` — origin story and “friend with a self” direction

## Status

- Main path runs; unit tests green.
- Tool path defaults to Pi; without Pi it fails honestly and does not pretend success.
- Introspection (300) and permissions (100 / 15 / 11) are early.
- Interfaces will change; not a stable dependency.

## License

[MIT](LICENSE)
