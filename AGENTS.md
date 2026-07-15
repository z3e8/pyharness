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
make run       # interactive agent + live viewer :6061 (needs ANTHROPIC_API_KEY in .env)
make watch     # live viewer alone (tails .sessions/) for a session started elsewhere
make up        # optional Phoenix OTel backend (:6006); make down stops it
```

Direct equivalents without make: `uv run pytest -q`, `uv run pyharness`. Config
lives in a single `.env` (copied from `.env.example`); the Makefile and
docker-compose both read it — do not export env vars by hand.

## Repo map

Folder names are visible from `ls`; this table is the *meaning* — where the
load-bearing seams are.

| Path | What lives here |
|------|-----------------|
| `pyharness/core/` | `session.py`, `agent.py`, `kernel.py`, `workspace.py`, `media.py` (image blocks back to the model), `session_venv.py` (per-session venv, out-of-process only) — the orchestration loop and persistent kernel |
| `pyharness/broker/` | `dispatch.py` — the single choke point every side effect routes through (policy → audit → budget → execute); `capabilities/` — one module per capability (agents, browser, exa, files, history, http, inbox, llm, notify, obs, packages, page, payload, search, secrets, shell, skills, tools, web); `remote/` — the out-of-process child (`child.py`, `host.py`, `protocol.py`, `sandbox.py`) |
| `pyharness/security/` | `policy.py`, `grants.py` (scoped approval grants), `vault.py`, `profiles.py` (encrypted browser login profiles), `totp.py` (RFC 6238 codes from vault seeds), `sink.py` (per-context secret masking) — action policy + the encrypted secrets vault |
| `pyharness/tools/` | `registry.py`, `skills.py` — tool discovery (`search_tools`/`use_tool`) and saved skills; `mcp/` — MCP server client/config/transport |
| `pyharness/llm/` | `client.py` — Anthropic client wrapper |
| `pyharness/` (top) | `audit.py` (hash-chained log), `budget.py`, `telemetry.py` (opt-in OTel export), `trace.py`, `watch.py` (live session viewer, `pyharness-watch`), `util.py`, `index.py` (derived SQLite session index), `reflect.py` + `lessons.py` (post-session self-improvement pass, opt-in), `cli.py`, `cli_vault.py`, `cli_profiles.py`, `cli_index.py`, `cli_mcp.py` |
| `tests/` | pytest suite; `mcp_server_fake.py` is a test double |
| `deploy/observability/` | docker-compose for the optional OTel backends: Phoenix, and Langfuse (heavier profile) |
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

## The agents/ folder

`agents/` is version-tracked working space for agents (Claude Code included) to
plan work that outlasts one session. If a task is too big to finish in one go,
drop the plan and running progress here as a living TODO so the next agent
resumes from it, and keep it current with what actually shipped. Keep it tidy:
prune stale notes, and move finished or superseded files into `agents/old/`.
It is committed (so plans survive across machines and sessions), but nothing in
it is load-bearing for the running code.

## Gotchas

- `docs-legacy/` is gitignored local-only notes — **do not treat it as current
  or wire anything to it.** The live docs are `docs/`; agent scratch is `agents/`
  (see above).
- Session state lives under `.sessions/` (gitignored). The agent's relative
  paths resolve inside its session workspace, not the repo root.
