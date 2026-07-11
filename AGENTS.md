# AGENTS.md

Context for AI coding agents working in this repo. Keep this file to
**non-inferable** facts (commands, layout, conventions, gotchas) — not things
you can read from the code. Universal working-style rules live in the global
`~/.claude/CLAUDE.md` (source: `~/code/skills/CLAUDE.md`), loaded automatically.

## What this is

`pyharness` — an AI agent whose **action space is Python**. The orchestrator
either replies with text or emits one `run_python` call that the harness runs in
a persistent Jupyter-style kernel. No fine-grained JSON tools; capabilities are
builtins in scope or tools imported on demand. See `docs/explanation/` for the
model.

## Workflow

1. **Understand** the code before changing it.
2. **Plan the new code**
3. **Write the new code and any tests for it** in `tests/`.
4. **Run `make test`** and fix anything that breaks.
5. **Update and/or add docs or CLAUDE.md as needed** 

## Commands

Everything is driven by `make` (`make help` lists all targets). `uv` is the
package manager; there is no `pip`/`venv` step to run by hand.

```bash
make setup     # create .env + install editable package + pytest (one-time)
make test      # run the test suite — no API key needed. Use this to verify changes.
make run       # interactive agent (needs ANTHROPIC_API_KEY in .env)
make dev       # observability (Phoenix, :6006) + the agent, one command
make down      # stop the Phoenix container
```

Direct equivalents without make: `uv run pytest -q`, `uv run pyharness`.

- **Tests require no API key** and are the primary verification gate. Run
  `make test` before considering a change done.
- Config lives in a single `.env` (copied from `.env.example`). The Makefile and
  docker-compose both read it — do not export env vars by hand.

## Repo map

| Path | What lives here |
|------|-----------------|
| `pyharness/core/` | `session.py`, `agent.py`, `kernel.py`, `workspace.py` — the orchestration loop and persistent kernel |
| `pyharness/broker/` | `dispatch.py` — the single choke point every side effect routes through (policy → audit → budget → execute) |
| `pyharness/security/` | `policy.py`, `vault.py` — action policy + the encrypted secrets vault |
| `pyharness/tools/` | `registry.py`, `skills.py` — tool discovery (`search_tools`/`use_tool`) and saved skills |
| `pyharness/llm/` | `client.py` — Anthropic client wrapper |
| `pyharness/` (top) | `audit.py` (hash-chained log), `budget.py`, `telemetry.py`, `trace.py`, `cli.py`, `cli_vault.py` |
| `tests/` | pytest suite; `mcp_server_fake.py` is a test double |
| `deploy/observability/` | docker-compose for Phoenix (default) and Langfuse (heavier profile) |
| `docs/` | documentation — explanation / how-to / reference (see `docs/index.md`) |
| `.claude/skills/docs/` | the `docs` skill: how to use and maintain the docs |

## Conventions

- Python ≥ 3.11. Match surrounding style; keep changes surgical (see the global
  working style).
- Every side effect the agent takes goes through `broker/dispatch.py` — add new
  capabilities there, not by scattering I/O across modules.
- The audit log is a tamper-evident hash chain; verify with
  `make verify-audit DIR=.sessions/<name>`.

## Docs

Live docs are `docs/` (explanation / how-to / reference). Consult them to
orient; keep them true. A change that alters behavior, a builtin, a flag, or a
config var must update the matching page **in the same commit** — the reference
pages track specific sources (`builtins.md` ↔ the `SYSTEM_PROMPT` in
`agent.py`, `configuration.md` ↔ `.env.example`, `python-api.md` ↔
`__init__.py`). Don't add speculative or broad-strokes pages; cut a page rather
than let it rot. The `docs` skill has the detail on when, where, and how.

## Gotchas

- `docs-legacy/` and `agents/` are gitignored local-only notes — **do not treat
  them as current or wire anything to them.** The live docs are `docs/`.
- Session state lives under `.sessions/` (gitignored). The agent's relative
  paths resolve inside its session workspace, not the repo root.
