# AGENTS.md

Context for AI coding agents working in this repo. Keep this file to
**non-inferable** facts (commands, layout, conventions, gotchas) — not things
you can read from the code. Universal working-style rules live in the global
`~/.claude/CLAUDE.md`, loaded automatically. The default change workflow lives
in the global `dev-workflow` skill; this file adds the pyharness-specific
commands, layout, and conventions it needs.

## What this is

`pyharness` — an AI agent whose **action space is Python**. The orchestrator
either replies with text or emits one `run_python` call that the harness runs in
a persistent Jupyter-style kernel. No fine-grained JSON tools; capabilities are
builtins in scope or tools imported on demand. See `docs/explanation/` for the
model.

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

Direct equivalents without make: `uv run pytest -q`, `uv run pyharness`. Config
lives in a single `.env` (copied from `.env.example`); the Makefile and
docker-compose both read it — do not export env vars by hand.

## Repo map

Folder names are visible from `ls`; this table is the *meaning* — where the
load-bearing seams are.

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

- Python ≥ 3.11. Match surrounding style; keep changes surgical.
- Every side effect the agent takes goes through `broker/dispatch.py` — add new
  capabilities there, not by scattering I/O across modules.
- The audit log is a tamper-evident hash chain; verify with
  `make verify-audit DIR=.sessions/<name>`.
- A change that alters behavior, a builtin, a flag, or a config var updates the
  matching `docs/` page in the same commit — reference pages track specific
  sources (`builtins.md` ↔ `SYSTEM_PROMPT` in `agent.py`, `configuration.md` ↔
  `.env.example`, `python-api.md` ↔ `__init__.py`). The `docs` skill has the
  full sync map.

## Gotchas

- `docs-legacy/` and `agents/` are gitignored local-only notes — **do not treat
  them as current or wire anything to them.** The live docs are `docs/`.
- Session state lives under `.sessions/` (gitignored). The agent's relative
  paths resolve inside its session workspace, not the repo root.
